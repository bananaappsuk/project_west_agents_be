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

import hashlib
import logging
import re
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


#: The CRM's controlled safeguarding vocabulary (their enums; mirrored in
#: agent_factory's prompt). A value outside this set is a 422 that loses the whole
#: referral, so anything unrecognised is dropped and the rest still goes.
_SAFEGUARDING_FLAGS = {
    "domestic_abuse", "child_abuse_neglect", "substance_misuse", "mental_health",
    "abduction_risk", "violence_or_aggression", "relevant_convictions",
}

#: A referral reference the CRM has already issued, quoted back at us in a reply —
#: a family returning a completed Part 2, or a bounce for mail the CRM sent them.
_REFERRAL_REF_RE = re.compile(r"\bPW-R-\d{4}-\d{3,}\b", re.IGNORECASE)


def _enum_or_none(value: str, allowed: set[str]) -> str | None:
    v = (value or "").strip().upper()
    return v if v in allowed else None


def _safeguarding_flags(values: list[str] | None) -> list[str]:
    """Normalise to the CRM's vocabulary, dropping anything it would reject.

    Their parser forgives casing and separators ("Domestic Abuse", "domestic-abuse"),
    so normalise here rather than relying on it. An unknown value is dropped rather
    than passed through: one bad entry 422s the entire referral, and losing a
    referral is worse than losing one flag off it.

    Deduplicated and ordered so the same email always produces the same payload.
    """
    out: set[str] = set()
    for v in values or []:
        norm = (v or "").strip().lower().replace("-", "_").replace(" ", "_")
        if norm in _SAFEGUARDING_FLAGS:
            out.add(norm)
    return sorted(out)


def _referral_ref_in(*texts: str | None) -> str | None:
    """The PW-R- reference an email quotes back, if any.

    Carried on the activity log as `outcome_reference` so a completed Part 2 form or
    a delivery failure reaches a coordinator attached to the referral it concerns,
    rather than as an unlinked note. The CRM has no endpoint for either yet; this is
    the shape they asked for in the meantime.
    """
    for t in texts:
        m = _REFERRAL_REF_RE.search(t or "")
        if m:
            return m.group(0).upper()
    return None


def _idempotency_key(uid: str) -> str:
    """A 64-character key derived from the Graph message id.

    The CRM's `idempotency_key` is capped at 120 characters and a Graph message id
    is 152, which rejected every referral we ever sent with a 422 while the same
    string passed as `external_ref` (capped at 160). That asymmetry is the whole
    reason activity rows arrived and referrals did not.

    Hashed rather than truncated, on the CRM's own advice and for a reason worth
    recording: a Graph id leads with the mailbox and folder and carries the
    message's own bytes at the END, so two messages in one mailbox share a long
    prefix. A clipped key could collide, and the CRM has a unique index on
    (source_system, idempotency_key) — the second referral would be read as a
    replay of the first and handed back the first one's reference. A 201, no
    error, and a real referral silently lost. Far worse than the 422.

    SHA-256 of the full id is deterministic, so a retry of the same email produces
    the same key and still dedupes, and collisions are not a practical concern.
    """
    return hashlib.sha256((uid or "").encode("utf-8")).hexdigest()


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
                "idempotency_key": _idempotency_key(uid),
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
                # Sets the case's risk level on conversion, so an omission files a
                # real risk as routine. Validated rather than trusted: one value
                # outside their list 422s the whole referral.
                "safeguarding_flags": _safeguarding_flags(analysis.get("safeguarding_flags")),
                "source_payload": {"raw_email_subject": subject, "thread_id": uid},
                "attachments": uploaded,
            }
            res = await crm_client.submit_referral(payload)
            crm_reference = res.get("reference")
            crm_status = "sent"
            outcome, outcome_reference = "REFERRAL_CREATED", crm_reference

        elif intent in _COMMUNICATION_INTENTS:
            case_ref = analysis.get("case_ref") or None

            if intent in ("RESCHEDULE", "CANCEL"):
                # A session/booking change goes through /intake/change-request and only
                # that endpoint — /intake/communication now 422s on request_type/
                # meeting_reference/etc., and refuses category CHANGE_OR_CANCEL_SESSION/
                # CANCELLATION outright (CRM Integration Guide v2 §4/§5, rule 2).
                meeting_reference = analysis.get("meeting_reference") or None
                if case_ref and meeting_reference:
                    payload = {
                        "case_ref": case_ref,
                        "meeting_reference": meeting_reference,
                        "request_type": intent,
                        "channel": "EMAIL",
                        "occurred_at": occurred_at,
                        "summary": summary or subject or "",
                        "subject": subject,
                        "participant_name": sender,
                        "participant_address": from_email,
                        "attachments": uploaded,
                        "external_ref": uid,
                        "source_payload": {"thread_id": uid},
                    }
                    if analysis.get("preferred_start_at"):
                        payload["preferred_start_at"] = analysis["preferred_start_at"]
                    res = await crm_client.submit_change_request(payload)
                    crm_status, crm_reference = "sent", case_ref
                    outcome = "CHANGE_REQUEST_RAISED"
                    outcome_reference = res.get("change_request_id") or case_ref
                else:
                    # Can't identify the session (or even the case) to raise a change
                    # against — rule 3: don't force it through §4, log the contact and
                    # let a coordinator resolve it instead of guessing.
                    log.info(
                        "%s downgraded to activity-only, missing %s: email uid=%s",
                        intent, "case_ref" if not case_ref else "meeting_reference", uid,
                    )
                    crm_status = "skipped"
                    outcome = "SKIPPED_NO_CASE"
            elif not case_ref:
                log.info("communication skipped, no case_ref found: email uid=%s", uid)
                crm_status = "skipped"
                outcome = "SKIPPED_NO_CASE"
            else:
                payload = {
                    "case_ref": case_ref,
                    "channel": "EMAIL",
                    "direction": "INBOUND",
                    "category": "EXISTING_CASE",
                    "occurred_at": occurred_at,
                    "subject": subject,
                    "summary": summary or subject or "",
                    "participant_name": sender,
                    "participant_address": from_email,
                    "attachments": uploaded,
                    "external_ref": uid,
                    "source_payload": {"thread_id": uid},
                }
                res = await crm_client.submit_communication(payload)
                crm_status, crm_reference = "sent", case_ref
                outcome, outcome_reference = "COMMUNICATION_LOGGED", case_ref
    except Exception as exc:
        log.warning("CRM submission failed: email uid=%s intent=%s: %s", uid, intent, exc)
        crm_status = "failed"
        # Per CRM_ACTIVITY_LOG_PROPOSAL.md §6.4: the CRM's outcome enum has no
        # FAILED value, and we agreed not to report failures through to them at
        # all (crm_status="failed" is visible on our own side only) — so this
        # still logs an activity row, just as NO_ACTION, not a made-up value.
        outcome, outcome_reference, case_ref = "NO_ACTION", None, None

    # A reply quoting a referral the CRM already issued — a family returning their
    # completed Part 2, or a bounce for mail the CRM sent them. Neither has an
    # endpoint yet, and both are invisible to the CRM otherwise: a bounce arrives
    # after SMTP accepted the message, so their own records show it as delivered.
    # Carrying the reference on the activity row is the shape the CRM asked for in
    # the meantime, and it puts the item in front of a coordinator attached to the
    # referral it concerns rather than as an unlinked note.
    if outcome_reference is None:
        outcome_reference = _referral_ref_in(subject, email_payload.get("body"))

    activity_ref = await _log_activity(
        occurred_at=occurred_at, summary=summary, outcome=outcome,
        outcome_reference=outcome_reference, case_ref=case_ref,
        sender=sender, from_email=from_email, uid=uid,
    )
    return crm_status, crm_reference, activity_ref
