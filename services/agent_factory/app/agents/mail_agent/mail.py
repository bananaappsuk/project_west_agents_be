"""mail-agent mail agent — native LangGraph.

Given one inbound email, produce {summary, category, priority, confidence} and flag
escalation when confidence is low. Single-turn today; runs on the same StateGraph +
checkpointer machinery the voice agent will use for multi-turn.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from ... import taxonomy
from ...base import BaseAgent
from ...config import settings
from ...llm import analyze_email
from ...registry import register


class MailState(TypedDict, total=False):
    email: dict       # {from, fromEmail, subject, body}
    analysis: dict    # MailAnalysis as a dict
    escalate: bool
    auto_sendable: bool
    route: str        # the queue this item belongs in (taxonomy.Queue)
    restricted: bool  # safeguarding — restricted handling, never an autonomous reply


def _analyze(state: MailState) -> dict:
    return {"analysis": analyze_email(state["email"]).model_dump()}


def _guardrail(state: MailState) -> dict:
    """Confidence is necessary but never sufficient. The brief's §7.1 reserves whole
    classes of decision for staff regardless of how sure the model is — referrals,
    payments, complaints, legal and scheduling changes — and safeguarding is barred
    outright (§5.5), so the category/flag check below is an absolute veto on top of
    the threshold rather than another input to it."""
    analysis = state.get("analysis") or {}
    category = analysis.get("category", "unclassified")
    flags = analysis.get("flags") or []
    confidence = analysis.get("confidence", 0.0)

    auto_sendable = (
        analysis.get("needs_reply", False)
        and confidence >= settings.auto_send_confidence_threshold
        and not taxonomy.blocks_auto_send(category, flags)
    )
    return {
        "escalate": confidence < settings.confidence_threshold or "uncertain" in flags,
        "auto_sendable": auto_sendable,
        "route": taxonomy.route_for(category, flags),
        "restricted": taxonomy.is_safeguarding(category, flags),
    }


@register
class MailAgent(BaseAgent):
    app = "mail-agent"
    key = "mail"

    def build(self):
        graph = StateGraph(MailState)
        graph.add_node("analyze", _analyze)
        graph.add_node("guardrail", _guardrail)
        graph.set_entry_point("analyze")
        graph.add_edge("analyze", "guardrail")
        graph.add_edge("guardrail", END)
        # MemorySaver now; swap for a Postgres checkpointer when voice/multi-turn lands.
        return graph.compile(checkpointer=MemorySaver())
