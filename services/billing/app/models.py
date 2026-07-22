from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _id() -> str:
    return str(uuid.uuid4())


class Plan(Base):
    """A plan in an application's catalog. `entitlements` holds per-app limits/features."""

    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    app_key: Mapped[str] = mapped_column(String, index=True)
    plan_id: Mapped[str] = mapped_column(String, unique=True, index=True)  # e.g. "rekroot.pro"
    name: Mapped[str] = mapped_column(String)
    entitlements: Mapped[dict] = mapped_column(JSON, default=dict)  # {"agent.invocations.month": 50000, ...}
    stripe_price_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Subscription(Base):
    """An organization's subscription to a plan, per application."""

    __tablename__ = "subscriptions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    org_id: Mapped[str] = mapped_column(String, index=True)
    app_key: Mapped[str] = mapped_column(String, index=True)
    plan_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active")  # active | past_due | canceled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("org_id", "app_key", name="uq_subscription_org_app"),)


class UsageEvent(Base):
    """One metered unit of consumption, emitted by the Agent Factory (or any service)."""

    __tablename__ = "usage_events"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    org_id: Mapped[str] = mapped_column(String, index=True)
    app_key: Mapped[str] = mapped_column(String, index=True)
    meter: Mapped[str] = mapped_column(String, index=True)  # invocation | input_tokens | voice_minute | ...
    quantity: Mapped[float] = mapped_column(Float, default=0)
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
