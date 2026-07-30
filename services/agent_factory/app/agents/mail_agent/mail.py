"""mail-agent mail agent — native LangGraph.

Given one inbound email, produce {summary, category, priority, confidence} and flag
escalation when confidence is low. Single-turn today; runs on the same StateGraph +
checkpointer machinery the voice agent will use for multi-turn.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from ...base import BaseAgent
from ...config import settings
from ...llm import analyze_email
from ...registry import register


class MailState(TypedDict, total=False):
    email: dict       # {from, fromEmail, subject, body}
    analysis: dict    # MailAnalysis as a dict
    escalate: bool


def _analyze(state: MailState) -> dict:
    return {"analysis": analyze_email(state["email"]).model_dump()}


def _guardrail(state: MailState) -> dict:
    confidence = (state.get("analysis") or {}).get("confidence", 0.0)
    return {"escalate": confidence < settings.confidence_threshold}


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
