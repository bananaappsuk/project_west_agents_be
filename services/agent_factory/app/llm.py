"""Model access for agents.

Structured email analysis via OpenAI. When the API key is a placeholder ("dummy",
empty, etc.), a deterministic heuristic stub is used instead so the whole pipeline
runs without a paid key — it switches to the live LLM automatically once a real
`sk-...` key is set.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .config import settings

_PLACEHOLDER_KEYS = {"", "dummy", "sk-dummy", "changeme", "none", "null"}


class MailAnalysis(BaseModel):
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
    return _analyze_llm(email) if _is_real_key() else _analyze_stub(email)


def _analyze_llm(email: dict) -> MailAnalysis:
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    # No temperature pinned — some reasoning models (gpt-5.x, o-series) reject non-default values.
    llm = ChatOpenAI(model=settings.openai_model, api_key=settings.openai_api_key)
    analyzer = llm.with_structured_output(MailAnalysis)
    system = (
        "You triage inbound customer email. Produce a one-line summary, a category "
        "(Billing, Support, Sales, Security, General), a priority (High, Medium, Low), "
        "whether a reply to the sender is warranted, and — if so — a short, professional "
        "suggested reply addressed to the sender by first name. Set confidence 0-1."
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
    return MailAnalysis(
        summary=summary, category=category, priority=priority,
        needs_reply=needs_reply, suggested_reply=suggested, confidence=0.5,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Voice / call-recording analysis (voice-agent.call)
# ─────────────────────────────────────────────────────────────────────────────

_CATEGORIES = ["Sales Enquiry", "Complaint", "Support", "Booking", "Billing", "General Enquiry"]


class CallAnalysis(BaseModel):
    summary: str = Field(description="One-line summary of what the call was about")
    category: Literal["Sales Enquiry", "Complaint", "Support", "Booking", "Billing", "General Enquiry"]
    priority: Literal["High", "Medium", "Low"]
    risk: Literal["High", "Medium", "Low"] = Field(description="Churn / escalation risk")
    sentiment: Literal["Positive", "Neutral", "Negative"]
    needs_reply: bool = Field(description="Whether a follow-up reply to the customer is warranted")
    suggested_reply: str = Field(default="", description="A drafted reply if one is needed, else empty")
    confidence: float = Field(ge=0.0, le=1.0, description="0-1 confidence in the classification")


def analyze_call(call: dict) -> CallAnalysis:
    return _analyze_call_llm(call) if _is_real_key() else _analyze_call_stub(call)


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
        "Set confidence 0-1."
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
    return CallAnalysis(
        summary=summary_map[category], category=category, priority=priority, risk=risk,
        sentiment=sentiment, needs_reply=needs_reply, suggested_reply=suggested, confidence=0.5,
    )
