"""Client for the Agent Factory mail agent."""

from __future__ import annotations

import httpx

from .config import settings


async def analyze_email(email_payload: dict) -> dict:
    """Return {"analysis": {summary, category, priority, needs_reply, suggested_reply,
    confidence}, "escalate": bool, "auto_sendable": bool}."""
    url = f"{settings.agent_factory_url}/agents/{settings.app_key}/mail/invoke"
    headers: dict[str, str] = {}
    if settings.agent_factory_internal_key:
        headers["X-Internal-Key"] = settings.agent_factory_internal_key
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json={"email": email_payload}, headers=headers)
        resp.raise_for_status()
        return resp.json()
