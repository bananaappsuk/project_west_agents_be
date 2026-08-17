"""Billing service — plans, subscriptions, usage metering, and Stripe payment.

Quota enforcement is the callers' job (west_agent/voice_agent check /entitlements
and post to /usage); this service owns the Plan/Subscription/UsageEvent data and
the Stripe Checkout/Portal/webhook integration that keeps Subscription in sync.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import stripe
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_common import Auth

from .config import settings
from .db import SessionLocal, engine, get_session
from .models import Base, Plan, Subscription, UsageEvent

log = logging.getLogger("billing")

auth = Auth(jwks_url=settings.auth_jwks_url, issuer=settings.auth_issuer)

# Default catalog seeded on startup if the `plans` table is empty — see §4 of
# LANDING_PAGE_AND_PAYMENTS_PLAN.md. Each tier covers both channels equally;
# stripe_price_id is left unset until real Stripe prices are created (Checkout
# refuses to run for a plan with no price configured, see /checkout-session).
DEFAULT_PLANS = [
    {
        "app_key": "mail-agent",
        "plan_id": "platform.trial",
        "name": "Trial",
        "entitlements": {"email.analyses.month": 500, "voice.minutes.month": 500, "mailboxes": 1},
    },
    {
        "app_key": "mail-agent",
        "plan_id": "platform.starter",
        "name": "Starter",
        "entitlements": {"email.analyses.month": 2000, "voice.minutes.month": 2000, "mailboxes": 1},
    },
    {
        "app_key": "mail-agent",
        "plan_id": "platform.growth",
        "name": "Growth",
        "entitlements": {"email.analyses.month": 10000, "voice.minutes.month": 10000, "mailboxes": 5},
    },
    {
        "app_key": "mail-agent",
        "plan_id": "platform.enterprise",
        "name": "Enterprise",
        "entitlements": {"email.analyses.month": None, "voice.minutes.month": None, "mailboxes": None},
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        if not await session.scalar(select(Plan)):
            for row in DEFAULT_PLANS:
                session.add(Plan(**row))
            await session.commit()
            log.info("seeded default plan catalog (%d plans)", len(DEFAULT_PLANS))
    yield
    await engine.dispose()


app = FastAPI(title="Billing Service", version="0.1.0", lifespan=lifespan)

# All business routes live under /billing so the gateway can route by prefix
# (see backend/services/gateway/app/main.py's ROUTES table) — same convention
# voice_agent uses for /voice. /health stays unprefixed like every other service.
router = APIRouter(prefix="/billing")


class PlanIn(BaseModel):
    app_key: str
    plan_id: str
    name: str
    entitlements: dict = {}
    stripe_price_id: str | None = None


class SubscribeIn(BaseModel):
    org_id: str
    app_key: str
    plan_id: str
    trial: bool = False


class UsageIn(BaseModel):
    org_id: str
    app_key: str
    meter: str
    quantity: float = 1
    agent_id: str | None = None


class CheckoutSessionIn(BaseModel):
    org_id: str
    app_key: str
    plan_id: str


class PortalSessionIn(BaseModel):
    org_id: str
    app_key: str


async def _require_admin_or_internal(
    authorization: str | None = Header(default=None),
    x_internal_key: str | None = Header(default=None),
) -> dict:
    """Platform admin JWT, or a trusted service-to-service call (e.g. Auth
    creating a trial Subscription right after register()). Same interim
    pattern as Agent Factory's X-Internal-Key — see backend/services/
    agent_factory/app/main.py."""
    if settings.internal_api_key and x_internal_key == settings.internal_api_key:
        return {"internal": True}
    claims = await auth.claims(authorization)
    if "platform:admin" not in (claims.get("scope") or "").split():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "requires platform:admin")
    return claims


@app.get("/health")
async def health():
    return {"status": "ok", "service": "billing"}


@router.post("/plans", status_code=status.HTTP_201_CREATED)
async def create_plan(
    body: PlanIn, claims: dict = Depends(_require_admin_or_internal), session: AsyncSession = Depends(get_session)
):
    if await session.scalar(select(Plan).where(Plan.plan_id == body.plan_id)):
        raise HTTPException(status.HTTP_409_CONFLICT, "plan_id already exists")
    plan = Plan(**body.model_dump())
    session.add(plan)
    await session.commit()
    return {"id": plan.id, "plan_id": plan.plan_id}


@router.get("/plans")
async def list_plans(app_key: str | None = None, session: AsyncSession = Depends(get_session)):
    stmt = select(Plan)
    if app_key:
        stmt = stmt.where(Plan.app_key == app_key)
    return [
        {
            "plan_id": p.plan_id,
            "app_key": p.app_key,
            "name": p.name,
            "entitlements": p.entitlements,
            "has_stripe_price": bool(p.stripe_price_id),
        }
        for p in await session.scalars(stmt)
    ]


class PlanPatchIn(BaseModel):
    stripe_price_id: str | None = None
    name: str | None = None
    entitlements: dict | None = None


@router.patch("/plans/{plan_id}")
async def update_plan(
    plan_id: str,
    body: PlanPatchIn,
    claims: dict = Depends(_require_admin_or_internal),
    session: AsyncSession = Depends(get_session),
):
    """Attach a real Stripe price (or tweak entitlements) on a seeded plan —
    the default catalog is seeded with stripe_price_id unset (see DEFAULT_PLANS),
    since real prices only exist once created in the Stripe dashboard."""
    plan = await session.scalar(select(Plan).where(Plan.plan_id == plan_id))
    if not plan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plan not found")
    if body.stripe_price_id is not None:
        plan.stripe_price_id = body.stripe_price_id
    if body.name is not None:
        plan.name = body.name
    if body.entitlements is not None:
        plan.entitlements = body.entitlements
    await session.commit()
    return {"plan_id": plan.plan_id, "has_stripe_price": bool(plan.stripe_price_id)}


@router.post("/subscriptions", status_code=status.HTTP_201_CREATED)
async def subscribe(
    body: SubscribeIn,
    claims: dict = Depends(_require_admin_or_internal),
    session: AsyncSession = Depends(get_session),
):
    """Directly grant an org a plan — used for admin overrides and for Auth's
    post-register() trial provisioning. Paid upgrades from a live Stripe
    Checkout go through the webhook handler below instead, not this route."""
    plan = await session.scalar(select(Plan).where(Plan.plan_id == body.plan_id))
    if not plan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plan not found")
    trial_ends_at = (
        datetime.now(timezone.utc) + timedelta(days=settings.trial_days) if body.trial else None
    )
    sub = await session.scalar(
        select(Subscription).where(Subscription.org_id == body.org_id, Subscription.app_key == body.app_key)
    )
    if sub:
        sub.plan_id = body.plan_id
        sub.status = "trialing" if body.trial else "active"
        sub.trial_ends_at = trial_ends_at
    else:
        sub = Subscription(
            org_id=body.org_id,
            app_key=body.app_key,
            plan_id=body.plan_id,
            status="trialing" if body.trial else "active",
            trial_ends_at=trial_ends_at,
        )
        session.add(sub)
    await session.commit()
    return {
        "id": sub.id,
        "org_id": sub.org_id,
        "app_key": sub.app_key,
        "plan_id": sub.plan_id,
        "status": sub.status,
        "trial_ends_at": sub.trial_ends_at,
    }


@router.get("/entitlements")
async def entitlements(
    org_id: str,
    app_key: str,
    x_internal_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    """Plan limits plus month-to-date usage per meter, so callers (mail_agent,
    voice_agent) can enforce quota without re-deriving usage themselves.

    Trusted internal call (mail_agent/voice_agent checking their own org), or a
    user JWT whose token org matches the org being queried, or platform:admin."""
    trusted = bool(settings.internal_api_key) and x_internal_key == settings.internal_api_key
    if not trusted:
        claims = await auth.claims(authorization)
        if claims.get("org") != org_id and "platform:admin" not in (claims.get("scope") or "").split():
            raise HTTPException(status.HTTP_403_FORBIDDEN, "cannot view billing for another org")

    sub = await session.scalar(
        select(Subscription).where(Subscription.org_id == org_id, Subscription.app_key == app_key)
    )
    if not sub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no subscription")
    plan = await session.scalar(select(Plan).where(Plan.plan_id == sub.plan_id))
    plan_entitlements = plan.entitlements if plan else {}

    effective_status = sub.status
    if sub.status == "trialing" and sub.trial_ends_at and sub.trial_ends_at <= datetime.now(timezone.utc):
        effective_status = "past_due"

    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    usage: dict[str, float] = {meter: 0.0 for meter in plan_entitlements}
    rows = await session.scalars(
        select(UsageEvent).where(
            UsageEvent.org_id == org_id,
            UsageEvent.app_key == app_key,
            UsageEvent.created_at >= month_start,
        )
    )
    for ev in rows:
        if ev.meter in usage:
            usage[ev.meter] += ev.quantity

    return {
        "plan_id": sub.plan_id,
        "status": effective_status,
        "trial_ends_at": sub.trial_ends_at,
        "entitlements": plan_entitlements,
        "usage": usage,
    }


@router.post("/usage", status_code=status.HTTP_201_CREATED)
async def record_usage(
    body: UsageIn,
    x_internal_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    # Trusted internal call (e.g. mail_agent/voice_agent recording their own org's
    # usage after a pipeline run) bypasses the user-JWT check — same pattern as
    # _require_admin_or_internal above.
    trusted = bool(settings.internal_api_key) and x_internal_key == settings.internal_api_key
    if not trusted:
        await auth.claims(authorization)
    session.add(UsageEvent(**body.model_dump()))
    await session.commit()
    return {"recorded": True}


# ---- Stripe: Checkout, Customer Portal, webhooks ---------------------------


async def _activate_from_checkout_session(data: dict, session: AsyncSession) -> Subscription | None:
    """Shared by the webhook handler and the synchronous /checkout-session/{id}/confirm
    fallback (see its docstring for why both exist). `data` is a Stripe checkout.session
    object (or the dict Session.retrieve() returns — same shape)."""
    meta = data.get("metadata") or {}
    org_id = meta.get("org_id") or data.get("client_reference_id")
    app_key = meta.get("app_key")
    plan_id = meta.get("plan_id")
    if not (org_id and app_key and plan_id):
        log.warning("checkout session missing org/app/plan metadata; ignoring")
        return None

    sub = await session.scalar(
        select(Subscription).where(Subscription.org_id == org_id, Subscription.app_key == app_key)
    )
    if not sub:
        sub = Subscription(org_id=org_id, app_key=app_key, plan_id=plan_id)
        session.add(sub)
    sub.plan_id = plan_id
    sub.status = "active"
    sub.stripe_customer_id = data.get("customer")
    sub.stripe_subscription_id = data.get("subscription")
    sub.trial_ends_at = None
    await session.commit()
    return sub


@router.post("/checkout-session")
async def create_checkout_session(
    body: CheckoutSessionIn,
    claims: dict = Depends(auth.claims),
    session: AsyncSession = Depends(get_session),
):
    """An org owner starts a paid upgrade. Only the org named in the token may
    buy a plan for itself — this is a self-serve endpoint, not admin-only."""
    if claims.get("org") != body.org_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cannot manage billing for another org")
    if not settings.stripe_secret_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Stripe is not configured")

    plan = await session.scalar(select(Plan).where(Plan.plan_id == body.plan_id))
    if not plan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plan not found")
    if not plan.stripe_price_id:
        raise HTTPException(status.HTTP_409_CONFLICT, f"plan '{plan.plan_id}' has no Stripe price configured yet")

    stripe.api_key = settings.stripe_secret_key
    sub = await session.scalar(
        select(Subscription).where(Subscription.org_id == body.org_id, Subscription.app_key == body.app_key)
    )

    kwargs: dict = dict(
        mode="subscription",
        line_items=[{"price": plan.stripe_price_id, "quantity": 1}],
        success_url=f"{settings.frontend_url}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.frontend_url}/billing/canceled",
        client_reference_id=body.org_id,
        metadata={"org_id": body.org_id, "app_key": body.app_key, "plan_id": body.plan_id},
    )
    if sub and sub.stripe_customer_id:
        kwargs["customer"] = sub.stripe_customer_id

    checkout_session = stripe.checkout.Session.create(**kwargs)
    return {"url": checkout_session.url}


@router.post("/checkout-session/{session_id}/confirm")
async def confirm_checkout_session(
    session_id: str,
    claims: dict = Depends(auth.claims),
    session: AsyncSession = Depends(get_session),
):
    """Synchronous fallback for activating a Subscription right after Checkout
    returns, instead of waiting on the webhook. Stripe can't reach `localhost`,
    so in local dev POST /webhooks/stripe never fires unless `stripe listen` is
    running — without this, a real successful payment would leave the org's
    plan un-upgraded. Called by the frontend's /billing/success page. Safe to
    keep even once the webhook is live: this just applies the same update a
    little earlier, from the user's own return trip instead of Stripe's async
    delivery, and is idempotent (re-confirming an already-active session is a
    no-op update)."""
    if not settings.stripe_secret_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Stripe is not configured")

    stripe.api_key = settings.stripe_secret_key
    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
    except stripe.error.StripeError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"checkout session not found: {exc}") from exc

    # dict(checkout_session) mis-iterates StripeObject in this SDK version (raises
    # KeyError: 0) — .to_dict() is the correct conversion.
    data = checkout_session.to_dict()
    meta = data.get("metadata") or {}
    if meta.get("org_id") != claims.get("org"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this checkout session belongs to another org")

    if data.get("payment_status") != "paid" and data.get("status") != "complete":
        return {"activated": False, "status": data.get("status"), "payment_status": data.get("payment_status")}

    sub = await _activate_from_checkout_session(data, session)
    if not sub:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "checkout session had no plan metadata")
    return {"activated": True, "plan_id": sub.plan_id, "status": sub.status}


@router.post("/portal-session")
async def create_portal_session(
    body: PortalSessionIn,
    claims: dict = Depends(auth.claims),
    session: AsyncSession = Depends(get_session),
):
    if claims.get("org") != body.org_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cannot manage billing for another org")
    if not settings.stripe_secret_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Stripe is not configured")

    sub = await session.scalar(
        select(Subscription).where(Subscription.org_id == body.org_id, Subscription.app_key == body.app_key)
    )
    if not sub or not sub.stripe_customer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no Stripe billing account for this org yet")

    stripe.api_key = settings.stripe_secret_key
    portal_session = stripe.billing_portal.Session.create(
        customer=sub.stripe_customer_id, return_url=f"{settings.frontend_url}/billing"
    )
    return {"url": portal_session.url}


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, session: AsyncSession = Depends(get_session)):
    if not settings.stripe_webhook_secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "webhook secret not configured")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid webhook: {exc}") from exc

    etype = event["type"]
    data = event["data"]["object"]
    log.info("stripe webhook: %s", etype)

    if etype == "checkout.session.completed":
        await _activate_from_checkout_session(data, session)

    elif etype == "customer.subscription.updated":
        sub = await session.scalar(
            select(Subscription).where(Subscription.stripe_subscription_id == data.get("id"))
        )
        if sub:
            stripe_status = data.get("status")
            sub.status = {
                "active": "active",
                "trialing": "trialing",
                "past_due": "past_due",
                "unpaid": "past_due",
                "canceled": "canceled",
                "incomplete_expired": "canceled",
            }.get(stripe_status, sub.status)
            await session.commit()

    elif etype == "customer.subscription.deleted":
        sub = await session.scalar(
            select(Subscription).where(Subscription.stripe_subscription_id == data.get("id"))
        )
        if sub:
            sub.status = "canceled"
            await session.commit()

    elif etype == "invoice.payment_failed":
        sub = await session.scalar(
            select(Subscription).where(Subscription.stripe_customer_id == data.get("customer"))
        )
        if sub:
            sub.status = "past_due"
            await session.commit()

    return {"received": True}


app.include_router(router)
