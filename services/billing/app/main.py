"""Billing service — plans, subscriptions, and usage metering.

MVP surface. Quota aggregation/enforcement and the Stripe integration (usage
records + webhooks) are added in the Billing phase; the shape is here.
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_common import Auth

from .config import settings
from .db import engine, get_session
from .models import Base, Plan, Subscription, UsageEvent

auth = Auth(jwks_url=settings.auth_jwks_url, issuer=settings.auth_issuer)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(title="Billing Service", version="0.1.0", lifespan=lifespan)


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


class UsageIn(BaseModel):
    org_id: str
    app_key: str
    meter: str
    quantity: float = 1
    agent_id: str | None = None


@app.get("/health")
async def health():
    return {"status": "ok", "service": "billing"}


@app.post("/plans", status_code=status.HTTP_201_CREATED)
async def create_plan(body: PlanIn, session: AsyncSession = Depends(get_session)):
    if await session.scalar(select(Plan).where(Plan.plan_id == body.plan_id)):
        raise HTTPException(status.HTTP_409_CONFLICT, "plan_id already exists")
    plan = Plan(**body.model_dump())
    session.add(plan)
    await session.commit()
    return {"id": plan.id, "plan_id": plan.plan_id}


@app.get("/plans")
async def list_plans(app_key: str | None = None, session: AsyncSession = Depends(get_session)):
    stmt = select(Plan)
    if app_key:
        stmt = stmt.where(Plan.app_key == app_key)
    return [
        {"plan_id": p.plan_id, "app_key": p.app_key, "name": p.name, "entitlements": p.entitlements}
        for p in await session.scalars(stmt)
    ]


@app.post("/subscriptions", status_code=status.HTTP_201_CREATED)
async def subscribe(body: SubscribeIn, session: AsyncSession = Depends(get_session)):
    plan = await session.scalar(select(Plan).where(Plan.plan_id == body.plan_id))
    if not plan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plan not found")
    sub = await session.scalar(
        select(Subscription).where(Subscription.org_id == body.org_id, Subscription.app_key == body.app_key)
    )
    if sub:
        sub.plan_id = body.plan_id
        sub.status = "active"
    else:
        sub = Subscription(org_id=body.org_id, app_key=body.app_key, plan_id=body.plan_id)
        session.add(sub)
    await session.commit()
    return {"id": sub.id, "org_id": sub.org_id, "app_key": sub.app_key, "plan_id": sub.plan_id}


@app.get("/entitlements")
async def entitlements(org_id: str, app_key: str, session: AsyncSession = Depends(get_session)):
    sub = await session.scalar(
        select(Subscription).where(Subscription.org_id == org_id, Subscription.app_key == app_key)
    )
    if not sub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no subscription")
    plan = await session.scalar(select(Plan).where(Plan.plan_id == sub.plan_id))
    return {"plan_id": sub.plan_id, "status": sub.status, "entitlements": plan.entitlements if plan else {}}


@app.post("/usage", status_code=status.HTTP_201_CREATED)
async def record_usage(
    body: UsageIn, claims: dict = Depends(auth.claims), session: AsyncSession = Depends(get_session)
):
    session.add(UsageEvent(**body.model_dump()))
    await session.commit()
    return {"recorded": True}
