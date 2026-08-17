"""Client for the Billing service's entitlement check + usage metering.

Fails open: if BILLING_URL is unset, or the billing service errors/is
unreachable, checks pass and usage recording is skipped silently. Billing
enforcement is an additive safety net, not (yet) something this service's
core email pipeline depends on to function — see LANDING_PAGE_AND_PAYMENTS_PLAN.md
§8, which deliberately orders "entitlement enforcement" last so trial orgs
aren't blocked by incomplete billing rollout.
"""

from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger("mail_agent.billing")

METER = "email.analyses.month"


class BillingBlocked(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


async def check_entitlement(org_id: str) -> None:
    """Raises BillingBlocked if the org's subscription is inactive or this
    month's email-analysis quota is already used up. Fails open on any
    billing-side error (unreachable, misconfigured, no subscription yet)."""
    if not settings.billing_url:
        return
    headers = {"X-Internal-Key": settings.billing_internal_key} if settings.billing_internal_key else {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.billing_url}/billing/entitlements",
                params={"org_id": org_id, "app_key": "mail-agent"},
                headers=headers,
            )
        if resp.status_code == 404:
            return  # no subscription yet — don't block, billing rollout may be incomplete
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("entitlement check failed open: %s", exc)
        return

    if data.get("status") in ("past_due", "canceled"):
        raise BillingBlocked("SUBSCRIPTION_EXPIRED", "subscription is not active")

    limit = (data.get("entitlements") or {}).get(METER)
    if limit is None:
        return  # unlimited
    used = (data.get("usage") or {}).get(METER, 0)
    if used >= limit:
        raise BillingBlocked("QUOTA_EXCEEDED", f"{METER} quota exhausted for this billing period")


async def record_usage(org_id: str, quantity: int) -> None:
    if not settings.billing_url or quantity <= 0:
        return
    headers = {"X-Internal-Key": settings.billing_internal_key} if settings.billing_internal_key else {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{settings.billing_url}/billing/usage",
                json={"org_id": org_id, "app_key": "mail-agent", "meter": METER, "quantity": quantity},
                headers=headers,
            )
    except Exception as exc:
        log.warning("usage recording failed (non-fatal): %s", exc)
