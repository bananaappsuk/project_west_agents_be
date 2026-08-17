"""Client for the Billing service — starts a trial Subscription right after a
new org registers. Same X-Internal-Key pattern as Agent Factory's client (see
backend/services/apps/mail_agent/app/agent_client.py)."""

from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger("auth.billing_client")


async def start_trial(org_id: str, app_key: str) -> None:
    """Best-effort: billing being unreachable must not block registration —
    it isn't on the critical path for using the product yet. Mirrors the
    SMTP-failure-swallow pattern in forgot_password()."""
    if not settings.billing_url:
        log.warning("start_trial: billing_url not configured — skipping trial provisioning")
        return
    headers: dict[str, str] = {}
    if settings.billing_internal_key:
        headers["X-Internal-Key"] = settings.billing_internal_key
    body = {
        "org_id": org_id,
        "app_key": app_key,
        "plan_id": settings.billing_trial_plan_id,
        "trial": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{settings.billing_url}/billing/subscriptions", json=body, headers=headers)
            resp.raise_for_status()
        log.info("start_trial: org=%s app_key=%s -> trial subscription created", org_id, app_key)
    except Exception as exc:
        log.warning("start_trial FAILED: org=%s app_key=%s billing_url=%s error=%r", org_id, app_key, settings.billing_url, exc)
