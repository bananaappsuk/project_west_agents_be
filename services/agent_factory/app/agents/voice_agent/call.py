"""voice-agent call agent — native LangGraph.

Given one call recording ({caller, phone, agent, duration, transcript}), produce
{summary, category, priority, risk, sentiment, needs_reply, suggested_reply, confidence}
and flag escalation when confidence is low. Runs on the same StateGraph + checkpointer
machinery as the mail agent; ready for multi-turn once a durable checkpointer lands.

The factory's generic invoke passes the call under the shared "email" input slot, so this
graph reads state["email"] as the call record.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from ...base import BaseAgent
from ...config import settings
from ...llm import analyze_call
from ...registry import register


class CallState(TypedDict, total=False):
    email: dict       # the call record (generic input slot from the invoke API)
    analysis: dict    # CallAnalysis as a dict
    escalate: bool


def _analyze(state: CallState) -> dict:
    return {"analysis": analyze_call(state["email"]).model_dump()}


def _guardrail(state: CallState) -> dict:
    confidence = (state.get("analysis") or {}).get("confidence", 0.0)
    return {"escalate": confidence < settings.confidence_threshold}


@register
class CallAgent(BaseAgent):
    app = "voice-agent"
    key = "call"

    def build(self):
        graph = StateGraph(CallState)
        graph.add_node("analyze", _analyze)
        graph.add_node("guardrail", _guardrail)
        graph.set_entry_point("analyze")
        graph.add_edge("analyze", "guardrail")
        graph.add_edge("guardrail", END)
        return graph.compile(checkpointer=MemorySaver())
