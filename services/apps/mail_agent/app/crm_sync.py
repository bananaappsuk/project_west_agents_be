"""Glue between one analyzed Email row and the CRM's intake API (crm_client.py).

Called from pipeline.py right after an email's analysis succeeds, and from
api.retry_summary when a previously-failed/skipped row is retried. Never raises —
a CRM problem only ever costs this one row's crm_status, exactly like a failed
Agent Factory call only costs summary_status (see pipeline.py's crash-safety notes).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import crm_client
from .config import settings
from .models import Email

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


async def after_email_analysis(email: Email, analysis: dict, attachments: list[dict] | None = None) -> tuple[str, str | None]:
    """Returns (crm_status, crm_reference). crm_status is "none" | "sent" | "skipped" | "failed"."""
    if not settings.crm_enabled:
        return "none", None

    intent = analysis.get("intent") or "NONE"
    if intent == "NONE":
        return "none", None

    uploaded = await _upload_attachments(attachments or [])

    try:
        if intent == "REFERRAL":
            payload = {
                "channel": "EMAIL_AGENT",
                "idempotency_key": email.uid,
                "referrer_name": analysis.get("referrer_name") or email.sender,
                "referrer_email": email.from_email,
                "referrer_type": _enum_or_none(analysis.get("referrer_type") or "SELF", _REFERRER_TYPES) or "SELF",
                "service_requested": _enum_or_none(analysis.get("service_requested") or "", _SERVICE_TYPES),
                "help_needed": analysis.get("help_needed") or email.subject or "Referral request",
                # children/attachments are plain (non-nullable) lists on the CRM's schema —
                # an empty list is the correct "none" value, never a JSON null.
                "children": analysis.get("children") or [],
                "child_lives_with": analysis.get("child_lives_with") or None,
                "requesting_adult_name": analysis.get("requesting_adult_name") or None,
                "relationship_to_child": analysis.get("relationship_to_child") or None,
                "source_payload": {"raw_email_subject": email.subject, "thread_id": email.uid},
                "attachments": uploaded,
            }
            res = await crm_client.submit_referral(payload)
            return "sent", res.get("reference")

        if intent in _COMMUNICATION_INTENTS:
            case_ref = analysis.get("case_ref") or ""
            if not case_ref:
                log.info("communication skipped, no case_ref found: email uid=%s", email.uid)
                return "skipped", None
            if intent in ("RESCHEDULE", "CANCEL") and not (analysis.get("meeting_reference") or ""):
                log.info("change request skipped, no meeting_reference found: email uid=%s", email.uid)
                return "skipped", None

            payload = {
                "case_ref": case_ref,
                "channel": "EMAIL",
                "direction": "INBOUND",
                "category": "CANCELLATION" if intent in ("RESCHEDULE", "CANCEL") else "GENERAL",
                # occurred_at is required by the CRM's schema — fall back to "now" for the
                # rare message with no parseable Date header rather than send a null.
                "occurred_at": (email.received_at or datetime.now(timezone.utc)).isoformat(),
                "subject": email.subject,
                "summary": analysis.get("summary") or email.subject or "",
                "participant_name": email.sender,
                "participant_address": email.from_email,
                "attachments": uploaded,
                "external_ref": email.uid,
                "source_payload": {"thread_id": email.uid},
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
        log.warning("CRM submission failed: email uid=%s intent=%s: %s", email.uid, intent, exc)
        return "failed", None
