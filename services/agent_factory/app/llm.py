"""Model access for agents.

Structured email analysis via OpenAI. When the API key is a placeholder ("dummy",
empty, etc.), a deterministic heuristic stub is used instead so the whole pipeline
runs without a paid key — it switches to the live LLM automatically once a real
`sk-...` key is set.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from .config import settings

_PLACEHOLDER_KEYS = {"", "dummy", "sk-dummy", "changeme", "none", "null"}

# ─────────────────────────────────────────────────────────────────────────────
# CRM intake routing — shared by mail and call analysis
#
# Both the Mail Agent and Voice Agent forward certain messages on to an external CRM
# (referrals, case communications, session reschedule/cancel requests). Rather than a
# second LLM call per item, these fields ride along on the existing analysis call —
# see CRM_MAIL_VOICE_AGENT_BUILD_SPEC.md's "one shared AI-analysis component" principle.
# `case_ref`/`meeting_reference` are always re-derived by regex after the model runs
# (see _find_case_ref/_find_meeting_ref below) — a rigid formatted code is more reliable
# to extract with a pattern match than to trust a model to transcribe verbatim.
# ─────────────────────────────────────────────────────────────────────────────

_CASE_REF_RE = re.compile(r"\bPW-\d{4}-\d{3,}\b", re.IGNORECASE)
_MEETING_REF_RE = re.compile(r"\bPW-M-\d{4,}\b", re.IGNORECASE)

# The CRM's actual enums (app/core/enums.py: ReferralSource, ServiceType) — the
# prompt must only offer values the CRM will accept, or a REFERRAL 422s outright.
_REFERRER_TYPES = "SELF, LOCAL_AUTHORITY, CAFCASS, SOLICITOR, FAMILY_MEDIATION, COURT, OTHER"
_SERVICE_TYPES = (
    "SUPERVISED_FAMILY_TIME, SUPPORTED_FAMILY_TIME, COMMUNITY_FAMILY_TIME, "
    "VIRTUAL_FAMILY_TIME, FAMILY_TIME_HANDOVER, INDIRECT_FAMILY_TIME, "
    "FAMILY_TIME_ASSESSMENT, PROGRESS_REVIEW, FAMILY_SUPPORT, MENTORING, "
    "PRE_VISIT_MEETING, or UNSPECIFIED if it's not clearly one of these"
)


def _find_case_ref(text: str) -> str:
    m = _CASE_REF_RE.search(text or "")
    return m.group(0).upper() if m else ""


def _find_meeting_ref(text: str) -> str:
    m = _MEETING_REF_RE.search(text or "")
    return m.group(0).upper() if m else ""


def _classify_intent_stub(text: str, case_ref: str) -> str:
    """Keyword heuristic used only in no-API-key stub mode. A real LLM key does this
    classification itself (see the system prompts below) — this is deliberately coarse,
    mirroring the rest of this file's stub-vs-LLM quality gradient."""
    t = (text or "").lower()
    session_words = ("session", "appointment", "contact", "visit", "meeting", "booking")
    if any(w in t for w in ("referral", "please refer", "refer us", "referring", "new referral")):
        return "REFERRAL"
    if any(w in t for w in ("reschedule", "move the session", "change the time", "different day", "different time")):
        return "RESCHEDULE"
    if any(w in t for w in ("cancel", "can't make", "won't be able", "will not be able")) and any(w in t for w in session_words):
        return "CANCEL"
    if case_ref:
        return "CASE_COMMUNICATION"
    return "NONE"


class ReferralChild(BaseModel):
    """A bare `dict` field breaks OpenAI's structured-output mode (it requires every
    nested object in the schema to declare additionalProperties: false, which only a
    named model — not a free-form dict — can do), so this is spelled out explicitly."""
    name: str = ""
    age: str = ""


class CrmFields(BaseModel):
    """Extracted whenever the message is a referral, a case update, or a session
    reschedule/cancel request — otherwise left at these defaults (intent="NONE")."""
    intent: Literal["REFERRAL", "CASE_COMMUNICATION", "RESCHEDULE", "CANCEL", "NONE"] = "NONE"
    case_ref: str = Field(default="", description="The case reference (e.g. PW-2026-0001) if mentioned, else empty")
    meeting_reference: str = Field(default="", description="The session/meeting reference (e.g. PW-M-000045) if mentioned, else empty")
    preferred_start_at: str = Field(default="", description="ISO 8601 datetime of a newly-requested session time, only for RESCHEDULE, else empty")
    referrer_name: str = Field(default="", description="Name of the person making the referral, if this is a REFERRAL")
    referrer_type: str = Field(default="", description="SELF, LOCAL_AUTHORITY, CAFCASS, SOLICITOR, FAMILY_MEDIATION, COURT, or OTHER — only for REFERRAL")
    service_requested: str = Field(default="", description="One of the CRM's service codes if it's clearly one of them, else UNSPECIFIED — only for REFERRAL")
    help_needed: str = Field(default="", description="What the referrer is asking for, only for REFERRAL")
    children: list[ReferralChild] = Field(default_factory=list, description="Children mentioned, only for REFERRAL")
    child_lives_with: str = Field(default="", description="Who the child currently lives with, only for REFERRAL")
    requesting_adult_name: str = Field(default="", description="Name of the requesting adult, only for REFERRAL")
    relationship_to_child: str = Field(default="", description="The requesting adult's relationship to the child, only for REFERRAL")


class MailAnalysis(CrmFields):
    summary: str = Field(description="One-line summary of what the email is asking for")
    category: str = Field(description="One of: Billing, Support, Sales, Security, General")
    priority: Literal["High", "Medium", "Low"]
    needs_reply: bool = Field(description="Whether this email warrants a reply to the sender")
    suggested_reply: str = Field(default="", description="A drafted reply if one is needed, else empty")
    confidence: float = Field(ge=0.0, le=1.0, description="0-1 confidence in the classification")


def _is_real_key() -> bool:
    key = (settings.openai_api_key or "").strip()
    return bool(key) and key.lower() not in _PLACEHOLDER_KEYS and key.startswith("sk-")


def analyze_email(email: dict) -> MailAnalysis:
    result = _analyze_llm(email) if _is_real_key() else _analyze_stub(email)
    text = f"{email.get('subject', '')} {email.get('body', '')}"
    result.case_ref = _find_case_ref(text) or result.case_ref
    result.meeting_reference = _find_meeting_ref(text) or result.meeting_reference
    return result


def _analyze_llm(email: dict) -> MailAnalysis:
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    # No temperature pinned — some reasoning models (gpt-5.x, o-series) reject non-default values.
    llm = ChatOpenAI(model=settings.openai_model, api_key=settings.openai_api_key)
    analyzer = llm.with_structured_output(MailAnalysis)
    system = (
        "You triage inbound customer email for a family-services organisation. Produce a "
        "one-line summary, a category (Billing, Support, Sales, Security, General), a "
        "priority (High, Medium, Low), whether a reply to the sender is warranted, and — "
        "if so — a short, professional suggested reply addressed to the sender by first "
        "name. Set confidence 0-1.\n\n"
        "Also classify `intent`, used to route this email to the CRM:\n"
        f"- REFERRAL: someone is asking to refer a family/child for a new service (not yet "
        f"an existing case). Fill referrer_name, referrer_type (one of: {_REFERRER_TYPES}), "
        f"service_requested (one of: {_SERVICE_TYPES}), help_needed, children, "
        "child_lives_with, requesting_adult_name, relationship_to_child from what is "
        "explicitly stated — leave any you can't find blank.\n"
        "- CASE_COMMUNICATION: an update, question, or general contact about an existing "
        "case (a case reference like PW-2026-0001 may be quoted).\n"
        "- RESCHEDULE: asking to move a session/appointment to a different time. Also set "
        "preferred_start_at (ISO 8601) if a new time is proposed.\n"
        "- CANCEL: asking to cancel a session/appointment.\n"
        "- NONE: none of the above.\n"
        "Never invent a case reference, meeting reference, or name that isn't literally in "
        "the message — leave the field blank instead."
    )
    human = (
        f"From: {email.get('from')} <{email.get('fromEmail')}>\n"
        f"Subject: {email.get('subject')}\n\n{email.get('body')}"
    )
    return analyzer.invoke([SystemMessage(content=system), HumanMessage(content=human)])


def _analyze_stub(email: dict) -> MailAnalysis:
    subject = (email.get("subject") or "").strip()
    body = (email.get("body") or "").strip()
    text = f"{subject} {body}".lower()

    if any(w in text for w in ("password", "login", "verify", "suspicious", "security", "breach")):
        category = "Security"
    elif any(w in text for w in ("invoice", "payment", "billing", "refund", "charge")):
        category = "Billing"
    elif any(w in text for w in ("help", "issue", "problem", "error", "not working", "support")):
        category = "Support"
    elif any(w in text for w in ("demo", "pricing", "quote", "interested", "purchase")):
        category = "Sales"
    else:
        category = "General"

    if category == "Security" or any(w in text for w in ("urgent", "asap", "immediately", "critical", "emergency")):
        priority = "High"
    elif "?" in body or "please" in text:
        priority = "Medium"
    else:
        priority = "Low"

    first_line = body.split("\n", 1)[0] if body else subject
    summary = (first_line or "(no content)")[:160]

    first_name = (email.get("from") or "there").split()[0]
    needs_reply = category != "General"
    suggested = ""
    if needs_reply:
        suggested = (
            f"Hi {first_name},\n\nThanks for reaching out regarding \"{subject}\". "
            "We've received your message and will get back to you shortly.\n\n"
            "Best regards,\nSupport Team"
        )
    case_ref = _find_case_ref(text)
    intent = _classify_intent_stub(text, case_ref)
    referrer_fields: dict = {}
    if intent == "REFERRAL":
        referrer_fields = {
            "referrer_name": email.get("from", ""),
            "referrer_type": "SELF",
            "help_needed": (first_line or subject)[:300],
        }
    return MailAnalysis(
        summary=summary, category=category, priority=priority,
        needs_reply=needs_reply, suggested_reply=suggested, confidence=0.5,
        intent=intent, case_ref=case_ref, **referrer_fields,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Voice / call-recording analysis (voice-agent.call)
# ─────────────────────────────────────────────────────────────────────────────

_CATEGORIES = ["Sales Enquiry", "Complaint", "Support", "Booking", "Billing", "General Enquiry"]


class CallAnalysis(CrmFields):
    summary: str = Field(description="One-line summary of what the call was about")
    category: Literal["Sales Enquiry", "Complaint", "Support", "Booking", "Billing", "General Enquiry"]
    priority: Literal["High", "Medium", "Low"]
    risk: Literal["High", "Medium", "Low"] = Field(description="Churn / escalation risk")
    sentiment: Literal["Positive", "Neutral", "Negative"]
    needs_reply: bool = Field(description="Whether a follow-up reply to the customer is warranted")
    suggested_reply: str = Field(default="", description="A drafted reply if one is needed, else empty")
    confidence: float = Field(ge=0.0, le=1.0, description="0-1 confidence in the classification")


def analyze_call(call: dict) -> CallAnalysis:
    result = _analyze_call_llm(call) if _is_real_key() else _analyze_call_stub(call)
    text = call.get("transcript") or ""
    result.case_ref = _find_case_ref(text) or result.case_ref
    result.meeting_reference = _find_meeting_ref(text) or result.meeting_reference
    return result


def _analyze_call_llm(call: dict) -> CallAnalysis:
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(model=settings.openai_model, api_key=settings.openai_api_key)
    analyzer = llm.with_structured_output(CallAnalysis)
    system = (
        "You triage recorded customer phone calls for a UK telecom support centre. "
        f"Choose exactly one category from: {', '.join(_CATEGORIES)}. Give a one-line summary, "
        "a priority (High/Medium/Low), a churn/escalation risk (High/Medium/Low), the customer "
        "sentiment (Positive/Neutral/Negative), whether a follow-up reply is needed, and — if so — "
        "a short, empathetic suggested reply addressed to the caller by first name. "
        "Set confidence 0-1.\n\n"
        "Also classify `intent`, used to route this call to the CRM:\n"
        f"- REFERRAL: caller is asking to refer a family/child for a new service (not an "
        f"existing case). Fill referrer_name, referrer_type (one of: {_REFERRER_TYPES}), "
        f"service_requested (one of: {_SERVICE_TYPES}), help_needed, children, "
        "child_lives_with, requesting_adult_name, relationship_to_child from what is "
        "explicitly said — leave any you can't find blank.\n"
        "- CASE_COMMUNICATION: an update, question, or general contact about an existing "
        "case (a case reference like PW-2026-0001 may be mentioned).\n"
        "- RESCHEDULE: asking to move a session/appointment to a different time. Also set "
        "preferred_start_at (ISO 8601) if a new time is proposed.\n"
        "- CANCEL: asking to cancel a session/appointment.\n"
        "- NONE: none of the above.\n"
        "Never invent a case reference, meeting reference, or name that isn't literally "
        "said — leave the field blank instead."
    )
    human = (
        f"Caller: {call.get('caller')} ({call.get('phone')})\n"
        f"Handled by: {call.get('agent')}\nDuration: {call.get('duration')}\n\n"
        f"Transcript excerpt:\n{call.get('transcript')}"
    )
    return analyzer.invoke([SystemMessage(content=system), HumanMessage(content=human)])


def _analyze_call_stub(call: dict) -> CallAnalysis:
    text = (call.get("transcript") or "").lower()
    caller = (call.get("caller") or "there").strip()
    first = caller.split()[0] if caller and caller != "Unknown Caller" else "there"

    if any(w in text for w in ("cancel", "missed", "wasted", "nobody", "second time", "hasn't shown", "not shown")):
        category = "Complaint"
    elif any(w in text for w in ("charge", "invoice", "payment", "refund", "tariff", "increase", "billing")):
        category = "Billing"
    elif any(w in text for w in ("plan", "upgrade", "pricing", "sign up", "install", "fibre", "interested")):
        category = "Sales Enquiry"
    elif any(w in text for w in ("book", "slot", "reminder", "appointment", "engineer", "tuesday", "friday")):
        category = "Booking"
    elif any(w in text for w in ("router", "reset", "connection", "lights", "voicemail", "pin", "not working", "cuts out")):
        category = "Support"
    else:
        category = "General Enquiry"

    resolved = any(w in text for w in ("thank", "brilliant", "perfect", "got it", "sorted", "worked", "understand", "helpful"))
    negative = category == "Complaint" or any(w in text for w in ("angry", "wasted", "cancel", "affecting"))
    sentiment = "Negative" if negative and not resolved else ("Positive" if resolved and not negative else "Neutral")

    if category == "Complaint":
        risk = "High"
    elif category in ("Billing", "Booking") and not resolved:
        risk = "Medium"
    else:
        risk = "Low"

    priority = "High" if risk == "High" else ("Medium" if (not resolved and category != "General Enquiry") else "Low")
    needs_reply = (not resolved) and category != "General Enquiry"

    summary_map = {
        "Complaint": "Customer raised a complaint and may be at risk of leaving.",
        "Billing": "Customer had a billing query that needs following up.",
        "Sales Enquiry": "Prospective customer enquired about products or upgrades.",
        "Booking": "Customer discussed an installation or appointment booking.",
        "Support": "Customer reported a technical issue.",
        "General Enquiry": "General enquiry, resolved on the call.",
    }
    suggested = ""
    if needs_reply:
        suggested = (
            f"Hi {first}, thanks for your call. I've noted everything we discussed and will make sure "
            "it's followed up promptly. If there's anything else you need in the meantime, just let us know."
        )
    case_ref = _find_case_ref(text)
    intent = _classify_intent_stub(text, case_ref)
    referrer_fields: dict = {}
    if intent == "REFERRAL":
        referrer_fields = {
            "referrer_name": caller if caller and caller != "Unknown Caller" else "",
            "referrer_type": "SELF",
            "help_needed": summary_map[category],
        }
    return CallAnalysis(
        summary=summary_map[category], category=category, priority=priority, risk=risk,
        sentiment=sentiment, needs_reply=needs_reply, suggested_reply=suggested, confidence=0.5,
        intent=intent, case_ref=case_ref, **referrer_fields,
    )
