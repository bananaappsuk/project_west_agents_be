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
    confidence: float = Field(ge=0.0, le=1.0, description="0-1 confidence in the classification")


def _is_real_key() -> bool:
    key = (settings.openai_api_key or "").strip()
    return bool(key) and key.lower() not in _PLACEHOLDER_KEYS and key.startswith("sk-")


def analyze_email(email: dict) -> MailAnalysis:
    return _analyze_llm(email) if _is_real_key() else _analyze_stub(email)


def _analyze_llm(email: dict) -> MailAnalysis:
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(model=settings.openai_model, api_key=settings.openai_api_key, temperature=0)
    analyzer = llm.with_structured_output(MailAnalysis)
    system = (
        "You triage inbound customer email. Produce a one-line summary, a category "
        "(Billing, Support, Sales, Security, General), a priority (High, Medium, Low), "
        "and your confidence from 0 to 1."
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
    return MailAnalysis(summary=summary, category=category, priority=priority, confidence=0.5)
