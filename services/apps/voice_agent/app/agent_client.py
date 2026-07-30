"""Client for the Agent Factory voice (call) agent."""

from __future__ import annotations

import httpx

from .config import settings


async def analyze_call(call_payload: dict) -> dict:
    """Return {"analysis": {summary, category, priority, risk, sentiment, needs_reply,
    suggested_reply, confidence}, "escalate": bool}.

    The factory's generic invoke wraps the payload under the "email" input slot; the
    voice agent's graph reads that slot as the call record.
    """
    url = f"{settings.agent_factory_url}/agents/{settings.app_key}/call/invoke"
    headers: dict[str, str] = {}
    if settings.agent_factory_internal_key:
        headers["X-Internal-Key"] = settings.agent_factory_internal_key
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json={"email": call_payload}, headers=headers)
        resp.raise_for_status()
        return resp.json()
