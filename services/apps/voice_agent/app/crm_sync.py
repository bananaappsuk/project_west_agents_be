"""Glue between one analyzed Recording row and the CRM's intake API (crm_client.py).

Called from pipeline.py right after a call's analysis succeeds. Never raises — a CRM
problem only ever costs this one row's crm_status, exactly like a failed Agent Factory
call only costs analysis_status (see pipeline.py's crash-safety notes).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import crm_client
from .config import settings
from .models import Recording

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


async def after_call_analysis(recording: Recording, analysis: dict) -> tuple[str, str | None]:
    """Returns (crm_status, crm_reference). crm_status is "none" | "sent" | "skipped" | "failed"."""
    if not settings.crm_enabled:
        return "none", None

    intent = analysis.get("intent") or "NONE"
    if intent == "NONE":
        return "none", None

    try:
        if intent == "REFERRAL":
            payload = {
                "channel": "VOICE_AGENT",
                "idempotency_key": recording.ext_id,
                "referrer_name": analysis.get("referrer_name") or recording.caller,
                "referrer_phone": recording.phone,
                "referrer_type": _enum_or_none(analysis.get("referrer_type") or "SELF", _REFERRER_TYPES) or "SELF",
                "service_requested": _enum_or_none(analysis.get("service_requested") or "", _SERVICE_TYPES),
                "help_needed": analysis.get("help_needed") or analysis.get("summary") or "Referral request",
                # children is a plain (non-nullable) list on the CRM's schema — an empty
                # list is the correct "none" value, never a JSON null.
                "children": analysis.get("children") or [],
                "child_lives_with": analysis.get("child_lives_with") or None,
                "requesting_adult_name": analysis.get("requesting_adult_name") or None,
                "relationship_to_child": analysis.get("relationship_to_child") or None,
                "source_payload": {"call_ext_id": recording.ext_id, "handled_by": recording.agent},
            }
            res = await crm_client.submit_referral(payload)
            return "sent", res.get("reference")

        if intent in _COMMUNICATION_INTENTS:
            case_ref = analysis.get("case_ref") or ""
            if not case_ref:
                log.info("communication skipped, no case_ref found: recording ext_id=%s", recording.ext_id)
                return "skipped", None
            if intent in ("RESCHEDULE", "CANCEL") and not (analysis.get("meeting_reference") or ""):
                log.info("change request skipped, no meeting_reference found: recording ext_id=%s", recording.ext_id)
                return "skipped", None

            # Recording.call_date is a date only (no time-of-day is captured upstream);
            # midnight UTC is an approximation, not the actual call time. occurred_at is
            # required by the CRM's schema, so fall back to "now" if call_date is somehow
            # blank rather than send a null.
            occurred_at = (
                f"{recording.call_date}T00:00:00Z" if recording.call_date
                else datetime.now(timezone.utc).isoformat()
            )
            payload = {
                "case_ref": case_ref,
                "channel": "CALL",
                "direction": "INBOUND",
                "category": "CANCELLATION" if intent in ("RESCHEDULE", "CANCEL") else "GENERAL",
                "occurred_at": occurred_at,
                "summary": analysis.get("summary") or "",
                "duration_seconds": _duration_seconds(recording.duration),
                "participant_name": recording.caller,
                "participant_address": recording.phone,
                "external_ref": recording.ext_id,
                "source_payload": {"handled_by": recording.agent},
            }
            if intent in ("RESCHEDULE", "CANCEL"):
                payload["meeting_reference"] = analysis["meeting_reference"]
                payload["request_type"] = intent
                if intent == "RESCHEDULE" and analysis.get("preferred_start_at"):
                    payload["preferred_start_at"] = analysis["preferred_start_at"]
            res = await crm_client.submit_communication(payload)
            return "sent", case_ref

        return "none", None
    except Exception as exc:
        log.warning("CRM submission failed: recording ext_id=%s intent=%s: %s", recording.ext_id, intent, exc)
        return "failed", None
