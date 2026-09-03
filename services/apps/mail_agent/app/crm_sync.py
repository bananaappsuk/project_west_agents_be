"""Glue between one analyzed Email row and the CRM's intake API (crm_client.py).

Called from pipeline.py right after an email's analysis succeeds, and from
api.retry_summary when a previously-failed/skipped row is retried. Never raises —
a CRM problem only ever costs this one row's crm_status/activity_ref, exactly like
a failed Agent Factory call only costs summary_status (see pipeline.py's
crash-safety notes).

POST /intake/activity is called once per email, always — see
CRM_ACTIVITY_LOG_PROPOSAL.md for the agreed contract. It's not a replacement for
submit_referral/submit_communication; it's the cross-referencing record that a
contact was handled at all, pointing at whichever of those also ran (or noting
that neither did, and why).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import crm_client
from .config import settings

log = logging.getLogger("mail_agent.crm_sync")

# Any intent other than these two is either "NONE" (nothing to forward) or an
# already-handled REFERRAL.
_COMMUNICATION_INTENTS = {"CASE_COMMUNICATION", "RESCHEDULE", "CANCEL"}

# The CRM's actual enums (pw-crm-be app/core/enums.py: ReferralSource, ServiceType).
# The LLM is prompted to only pick from these, but a bad/edited value would 422 the
# whole referral if sent through unvalidated — better to drop it and let the CRM's
# own missing_fields tracking flag the gap than lose the referral outright.
_REFERRER_TYPES = {"SELF", "LOCAL_AUTHORITY", "CAFCASS", "SOLICITOR", "FAMILY_MEDIATION", "COURT", "OTHER"}
_SERVICE_TYPES = {
    "SUPERVISED_FAMILY_TIME", "SUPPORTED_FAMILY_TIME", "COMMUNITY_FAMILY_TIME",
    "VIRTUAL_FAMILY_TIME", "FAMILY_TIME_HANDOVER", "INDIRECT_FAMILY_TIME",
    "FAMILY_TIME_ASSESSMENT", "PROGRESS_REVIEW", "FAMILY_SUPPORT", "MENTORING",
    "PRE_VISIT_MEETING", "UNSPECIFIED",
}


def _enum_or_none(value: str, allowed: set[str]) -> str | None:
    v = (value or "").strip().upper()
    return v if v in allowed else None


async def _upload_attachments(attachments: list[dict]) -> list[dict]:
    out = []
    for a in attachments or []:
        try:
            res = await crm_client.upload_attachment(a["filename"], a["data"], a.get("content_type") or "application/octet-stream")
            out.append(res)
        except Exception as exc:
            log.warning("attachment upload failed, skipping: %s (%s)", a.get("filename"), exc)
    return out


async def _log_activity(*, occurred_at: str, summary: str, outcome: str,
                         outcome_reference: str | None, case_ref: str | None,
                         sender: str, from_email: str, uid: str) -> str | None:
    """POST /intake/activity — best-effort, never raises. Returns activity_ref or None.
    Called once per email regardless of outcome (see module docstring)."""
    payload = {
        "agent": "EMAIL_AGENT",
        "contact_channel": "EMAIL",
        "occurred_at": occurred_at,
        "summary": summary or "(no summary)",
        "outcome": outcome,
        "outcome_reference": outcome_reference,
        "case_ref": case_ref,
        # Real sender name/address only — never a phone-shaped placeholder from a
        # different channel (the CRM team flagged exactly this mistake while
        # testing their own example payloads; this is the email side, no phone).
        "participant_name": sender or None,
        "participant_email": from_email or None,
        "external_ref": uid,
    }
    try:
        res = await crm_client.submit_activity(payload)
        return res.get("activity_ref")
    except Exception as exc:
        log.warning("activity log FAILED: email uid=%s outcome=%s: %s", uid, outcome, exc)
        return None


async def after_email_analysis(
    email_payload: dict, analysis: dict, attachments: list[dict] | None = None
) -> tuple[str, str | None, str | None]:
    """Returns (crm_status, crm_reference, activity_ref). crm_status is
    "none" | "sent" | "skipped" | "failed" — the referral/communication outcome.
    activity_ref is the separate POST /intake/activity reference, set whenever
    CRM sync is enabled at all (independent of crm_status).

    Takes the same serialized dict (schemas.serialize_email) pipeline.py already
    carries around for the LLM call, rather than the live `Email` ORM row —
    deliberately: this function makes slow HTTP calls (attachment upload,
    referral/communication/activity submission), and taking a plain dict instead
    of an attached ORM object means the caller can run it fully outside any open
    DB transaction/session (see pipeline.py's analyze_and_persist), never holding
    a pooled connection for however long the CRM API takes to respond."""
    if not settings.crm_enabled:
        return "none", None, None

    intent = analysis.get("intent") or "NONE"
    uid = email_payload.get("uid")
    subject = email_payload.get("subject")
    sender = email_payload.get("from")
    from_email = email_payload.get("fromEmail")
    received_at = email_payload.get("receivedAt")  # already an ISO string, or None
    summary = analysis.get("summary") or ""

    occurred_at = received_at or datetime.now(timezone.utc).isoformat()

    outcome = "NO_ACTION"
    outcome_reference: str | None = None
    case_ref: str | None = None
    crm_status, crm_reference = "none", None

    uploaded = await _upload_attachments(attachments or [])

    try:
        if intent == "REFERRAL":
            payload = {
                "channel": "EMAIL_AGENT",
                "idempotency_key": uid,
                "referrer_name": analysis.get("referrer_name") or sender,
                "referrer_email": from_email,
                "referrer_type": _enum_or_none(analysis.get("referrer_type") or "SELF", _REFERRER_TYPES) or "SELF",
                "service_requested": _enum_or_none(analysis.get("service_requested") or "", _SERVICE_TYPES),
                "help_needed": analysis.get("help_needed") or subject or "Referral request",
                # children/attachments are plain (non-nullable) lists on the CRM's schema —
                # an empty list is the correct "none" value, never a JSON null.
                "children": analysis.get("children") or [],
                "child_lives_with": analysis.get("child_lives_with") or None,
                "requesting_adult_name": analysis.get("requesting_adult_name") or None,
                "relationship_to_child": analysis.get("relationship_to_child") or None,
                "source_payload": {"raw_email_subject": subject, "thread_id": uid},
                "attachments": uploaded,
            }
            res = await crm_client.submit_referral(payload)
            crm_reference = res.get("submission_ref")
            crm_status = "sent"
            outcome, outcome_reference = "REFERRAL_CREATED", crm_reference

        elif intent in _COMMUNICATION_INTENTS:
            case_ref = analysis.get("case_ref") or None
            if not case_ref:
                log.info("communication skipped, no case_ref found: email uid=%s", uid)
                crm_status = "skipped"
                outcome = "SKIPPED_NO_CASE"
            else:
                is_change = intent in ("RESCHEDULE", "CANCEL") and bool(analysis.get("meeting_reference"))
                payload = {
                    "case_ref": case_ref,
                    "channel": "EMAIL",
                    "direction": "INBOUND",
                    "category": "CHANGE_OR_CANCEL_SESSION" if is_change else "EXISTING_CASE",
                    "occurred_at": occurred_at,
                    "subject": subject,
                    "summary": summary or subject or "",
                    "participant_name": sender,
                    "participant_address": from_email,
                    "attachments": uploaded,
                    "external_ref": uid,
                    "source_payload": {"thread_id": uid},
                }
                if is_change:
                    payload["meeting_reference"] = analysis["meeting_reference"]
                    payload["request_type"] = intent
                    if intent == "RESCHEDULE" and analysis.get("preferred_start_at"):
                        payload["preferred_start_at"] = analysis["preferred_start_at"]
                elif intent in ("RESCHEDULE", "CANCEL"):
                    # Case is known but the AI couldn't pin down which meeting —
                    # log it as a plain communication against the case rather than
                    # dropping it outright; a coordinator still needs to see it.
                    log.info(
                        "change request downgraded to plain communication, no meeting_reference: email uid=%s",
                        uid,
                    )

                res = await crm_client.submit_communication(payload)
                crm_status, crm_reference = "sent", case_ref
                if is_change:
                    outcome = "CHANGE_REQUEST_RAISED"
                    outcome_reference = res.get("change_request_id") or case_ref
                else:
                    outcome, outcome_reference = "COMMUNICATION_LOGGED", case_ref
    except Exception as exc:
        log.warning("CRM submission failed: email uid=%s intent=%s: %s", uid, intent, exc)
        crm_status = "failed"
        # Per CRM_ACTIVITY_LOG_PROPOSAL.md §6.4: the CRM's outcome enum has no
        # FAILED value, and we agreed not to report failures through to them at
        # all (crm_status="failed" is visible on our own side only) — so this
        # still logs an activity row, just as NO_ACTION, not a made-up value.
        outcome, outcome_reference, case_ref = "NO_ACTION", None, None

    activity_ref = await _log_activity(
        occurred_at=occurred_at, summary=summary, outcome=outcome,
        outcome_reference=outcome_reference, case_ref=case_ref,
        sender=sender, from_email=from_email, uid=uid,
    )
    return crm_status, crm_reference, activity_ref
