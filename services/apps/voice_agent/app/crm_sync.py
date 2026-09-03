"""Glue between one analyzed Recording row and the CRM's intake API (crm_client.py).

Called from pipeline.py right after a call's analysis succeeds. Never raises — a CRM
problem only ever costs this one row's crm_status/activity_ref, exactly like a failed
Agent Factory call only costs analysis_status (see pipeline.py's crash-safety notes).

POST /intake/activity is called once per recording, always — see
CRM_ACTIVITY_LOG_PROPOSAL.md for the agreed contract. It's not a replacement for
submit_referral/submit_communication; it's the cross-referencing record that a call
was handled at all, pointing at whichever of those also ran (or noting that neither
did, and why).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import crm_client
from .config import settings

log = logging.getLogger("voice_agent.crm_sync")

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


def _duration_seconds(duration: str) -> int | None:
    """Parses the stored "M:SS" (or "H:MM:SS") display string back to seconds."""
    if not duration:
        return None
    try:
        parts = [int(p) for p in duration.split(":")]
    except ValueError:
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


async def _log_activity(*, occurred_at: str, summary: str, outcome: str,
                         outcome_reference: str | None, case_ref: str | None,
                         caller: str, phone: str, duration: str, ext_id: str) -> str | None:
    """POST /intake/activity — best-effort, never raises. Returns activity_ref or None.
    Called once per recording regardless of outcome (see module docstring)."""
    payload = {
        "agent": "VOICE_AGENT",
        "contact_channel": "CALL",
        "occurred_at": occurred_at,
        "summary": summary or "(no summary)",
        "outcome": outcome,
        "outcome_reference": outcome_reference,
        "case_ref": case_ref,
        # Real caller ID/name only — never a phone-shaped placeholder borrowed from a
        # different channel (per the CRM team's own warning while testing this).
        "participant_name": caller or None,
        "participant_phone": phone or None,
        "duration_seconds": _duration_seconds(duration),
        "external_ref": ext_id,
    }
    try:
        res = await crm_client.submit_activity(payload)
        return res.get("activity_ref")
    except Exception as exc:
        log.warning("activity log FAILED: recording ext_id=%s outcome=%s: %s", ext_id, outcome, exc)
        return None


async def after_call_analysis(recording_payload: dict, analysis: dict) -> tuple[str, str | None, str | None]:
    """Returns (crm_status, crm_reference, activity_ref). crm_status is
    "none" | "sent" | "skipped" | "failed" — the referral/communication outcome.
    activity_ref is the separate POST /intake/activity reference, set whenever
    CRM sync is enabled at all (independent of crm_status).

    Takes a plain dict (the same one pipeline.py already carries around for the
    LLM call, plus ext_id/call_date) rather than the live `Recording` ORM row —
    deliberately: this makes HTTP calls to the CRM, and taking a dict instead
    of an attached ORM object means the caller can run it fully outside any open
    DB transaction/session (see pipeline.py's `_run`), never holding a pooled
    connection open for however long the CRM API takes to respond."""
    if not settings.crm_enabled:
        return "none", None, None

    intent = analysis.get("intent") or "NONE"
    ext_id = recording_payload.get("ext_id")
    caller = recording_payload.get("caller")
    phone = recording_payload.get("phone")
    agent = recording_payload.get("agent")
    call_date = recording_payload.get("call_date")
    duration = recording_payload.get("duration")
    summary = analysis.get("summary") or ""

    # call_date is a date only (no time-of-day is captured upstream); midnight
    # UTC is an approximation, not the actual call time. occurred_at is
    # required by the CRM's schema, so fall back to "now" if call_date is
    # somehow blank rather than send a null.
    occurred_at = (
        f"{call_date}T00:00:00Z" if call_date
        else datetime.now(timezone.utc).isoformat()
    )

    outcome = "NO_ACTION"
    outcome_reference: str | None = None
    case_ref: str | None = None
    crm_status, crm_reference = "none", None

    try:
        if intent == "REFERRAL":
            payload = {
                "channel": "VOICE_AGENT",
                "idempotency_key": ext_id,
                "referrer_name": analysis.get("referrer_name") or caller,
                "referrer_phone": phone,
                "referrer_type": _enum_or_none(analysis.get("referrer_type") or "SELF", _REFERRER_TYPES) or "SELF",
                "service_requested": _enum_or_none(analysis.get("service_requested") or "", _SERVICE_TYPES),
                "help_needed": analysis.get("help_needed") or summary or "Referral request",
                # children is a plain (non-nullable) list on the CRM's schema — an empty
                # list is the correct "none" value, never a JSON null.
                "children": analysis.get("children") or [],
                "child_lives_with": analysis.get("child_lives_with") or None,
                "requesting_adult_name": analysis.get("requesting_adult_name") or None,
                "relationship_to_child": analysis.get("relationship_to_child") or None,
                "source_payload": {"call_ext_id": ext_id, "handled_by": agent},
            }
            res = await crm_client.submit_referral(payload)
            crm_reference = res.get("submission_ref")
            crm_status = "sent"
            outcome, outcome_reference = "REFERRAL_CREATED", crm_reference

        elif intent in _COMMUNICATION_INTENTS:
            case_ref = analysis.get("case_ref") or None
            if not case_ref:
                log.info("communication skipped, no case_ref found: recording ext_id=%s", ext_id)
                crm_status = "skipped"
                outcome = "SKIPPED_NO_CASE"
            else:
                is_change = intent in ("RESCHEDULE", "CANCEL") and bool(analysis.get("meeting_reference"))
                payload = {
                    "case_ref": case_ref,
                    "channel": "CALL",
                    "direction": "INBOUND",
                    "category": "CHANGE_OR_CANCEL_SESSION" if is_change else "EXISTING_CASE",
                    "occurred_at": occurred_at,
                    "summary": summary,
                    "duration_seconds": _duration_seconds(duration),
                    "participant_name": caller,
                    "participant_address": phone,
                    "external_ref": ext_id,
                    "source_payload": {"handled_by": agent},
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
                        "change request downgraded to plain communication, no meeting_reference: recording ext_id=%s",
                        ext_id,
                    )

                res = await crm_client.submit_communication(payload)
                crm_status, crm_reference = "sent", case_ref
                if is_change:
                    outcome = "CHANGE_REQUEST_RAISED"
                    outcome_reference = res.get("change_request_id") or case_ref
                else:
                    outcome, outcome_reference = "COMMUNICATION_LOGGED", case_ref
    except Exception as exc:
        log.warning("CRM submission failed: recording ext_id=%s intent=%s: %s", ext_id, intent, exc)
        crm_status = "failed"
        # Per CRM_ACTIVITY_LOG_PROPOSAL.md §6.4: the CRM's outcome enum has no
        # FAILED value, and we agreed not to report failures through to them at
        # all (crm_status="failed" is visible on our own side only) — so this
        # still logs an activity row, just as NO_ACTION, not a made-up value.
        outcome, outcome_reference, case_ref = "NO_ACTION", None, None

    activity_ref = await _log_activity(
        occurred_at=occurred_at, summary=summary, outcome=outcome,
        outcome_reference=outcome_reference, case_ref=case_ref,
        caller=caller, phone=phone, duration=duration, ext_id=ext_id,
    )
    return crm_status, crm_reference, activity_ref
