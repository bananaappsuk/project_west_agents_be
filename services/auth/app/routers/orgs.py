"""Org-scoped, self-serve membership management for an application.

Unlike /admin (which needs platform:admin), these routes are scoped to the caller's
own org + app (taken from their token) and require the app-admin scope
(`{app}:admin`). This is what powers an application's "Users & Roles" screen:
list teammates, invite, change role, remove — without platform-level access.

Additive: does not touch the existing auth/admin routers. Role labels map to internal
role names + permission sets so each app's service RBAC keeps working:
    Superadmin -> owner  ({app}:admin, {app}:agent.invoke)   [full access via admin bypass]
    Admin      -> admin  (read + write + agent.invoke)
    User       -> user   (read only)
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_session
from ..deps import current_claims
from ..mailer import send_invite_email
from ..models import Application, Invite, Membership, Organization, Role, User
from ..security import hash_password, hash_token
from .auth import _ensure_permissions

INVITE_TTL_DAYS = 7

router = APIRouter(prefix="/orgs", tags=["orgs"])

DISPLAY_TO_INTERNAL = {"Superadmin": "owner", "Admin": "admin", "User": "user"}
INTERNAL_TO_DISPLAY = {"owner": "Superadmin", "admin": "Admin", "user": "User"}


class InviteIn(BaseModel):
    email: EmailStr
    role: str = "User"
    full_name: str | None = None
    # The inviting frontend's origin (e.g. http://localhost:3100), used to build the
    # accept link that gets emailed. Optional — if omitted, no email is attempted.
    accept_base: str | None = None


class RoleUpdateIn(BaseModel):
    role: str


def _perm_keys(app_key: str, internal: str) -> list[str]:
    if internal == "owner":
        return [f"{app_key}:admin", f"{app_key}:agent.invoke"]
    if internal == "admin":
        return [
            f"{app_key}:agent.invoke",
            f"{app_key}:recordings.read", f"{app_key}:recordings.write",
            f"{app_key}:emails.read", f"{app_key}:emails.write",
        ]
    # user (read-only)
    return [f"{app_key}:recordings.read", f"{app_key}:emails.read"]


def _display(role_names: list[str]) -> str:
    for internal in ("owner", "admin", "user"):
        if internal in role_names:
            return INTERNAL_TO_DISPLAY[internal]
    return INTERNAL_TO_DISPLAY.get(role_names[0], role_names[0].capitalize()) if role_names else "User"


async def _require_app_admin(claims: dict = Depends(current_claims)) -> dict:
    granted = set((claims.get("scope") or "").split())
    app_key = claims.get("aud") or ""
    if "platform:admin" in granted or f"{app_key}:admin" in granted:
        return claims
    raise HTTPException(status.HTTP_403_FORBIDDEN, "requires app admin")


async def _app(session: AsyncSession, app_key: str) -> Application:
    app = await session.scalar(select(Application).where(Application.key == app_key))
    if not app:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "application not registered")
    return app


async def _ensure_role(session: AsyncSession, app: Application, org_id: str, display_role: str) -> Role:
    internal = DISPLAY_TO_INTERNAL.get(display_role)
    if not internal:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown role: {display_role}")
    role = await session.scalar(
        select(Role).where(Role.app_id == app.id, Role.org_id == org_id, Role.name == internal)
    )
    if role is None:
        perms = await _ensure_permissions(session, app, _perm_keys(app.key, internal))
        role = Role(name=internal, app_id=app.id, org_id=org_id, permissions=perms)
        session.add(role)
        await session.flush()
    return role


def _member_dict(user: User, membership: Membership) -> dict:
    return {
        "id": user.id,
        "name": user.full_name or user.email.split("@")[0],
        "email": user.email,
        "role": _display([r.name for r in membership.roles]),
        "status": "active" if user.is_active else "invited",
    }


@router.get("/members")
async def list_members(claims: dict = Depends(_require_app_admin), session: AsyncSession = Depends(get_session)):
    app = await _app(session, claims["aud"])
    rows = list(await session.scalars(
        select(Membership)
        .where(Membership.org_id == claims["org"], Membership.app_id == app.id)
        .options(selectinload(Membership.roles))
    ))
    out = []
    for m in rows:
        user = await session.get(User, m.user_id)
        if user:
            out.append(_member_dict(user, m))
    out.sort(key=lambda x: (x["status"] != "active", x["name"].lower()))
    return out


@router.post("/members", status_code=status.HTTP_201_CREATED)
async def invite_member(body: InviteIn, claims: dict = Depends(_require_app_admin), session: AsyncSession = Depends(get_session)):
    app = await _app(session, claims["aud"])
    org_id = claims["org"]
    role = await _ensure_role(session, app, org_id, body.role)

    user = await session.scalar(select(User).where(User.email == body.email))
    if user is None:
        name = body.full_name or body.email.split("@")[0].replace(".", " ").replace("_", " ").title()
        # Invited users are inactive until they accept (set a password); they can't log in yet.
        user = User(email=body.email, password_hash=hash_password(uuid.uuid4().hex), full_name=name, is_active=False)
        session.add(user)
        await session.flush()

    membership = await session.scalar(
        select(Membership)
        .where(Membership.user_id == user.id, Membership.org_id == org_id, Membership.app_id == app.id)
        .options(selectinload(Membership.roles))
    )
    if membership is None:
        membership = Membership(user_id=user.id, org_id=org_id, app_id=app.id, roles=[role])
        session.add(membership)
    else:
        membership.roles = [role]

    # New / not-yet-activated users get a one-time acceptance token (returned once, so the
    # inviter can share the link — there is no email delivery yet).
    invite_token: str | None = None
    if not user.is_active:
        invite_token = secrets.token_urlsafe(32)
        session.add(Invite(
            user_id=user.id, org_id=org_id, app_id=app.id, token_hash=hash_token(invite_token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=INVITE_TTL_DAYS),
        ))

    await session.commit()
    await session.refresh(membership, ["roles"])
    out = _member_dict(user, membership)

    if invite_token:
        out["inviteToken"] = invite_token
        emailed = False
        if body.accept_base:
            link = f"{body.accept_base.rstrip('/')}/accept?token={invite_token}"
            org = await session.get(Organization, org_id)
            try:
                emailed = await run_in_threadpool(
                    send_invite_email,
                    to=user.email,
                    org_name=org.name if org else "your team",
                    role=body.role,
                    accept_link=link,
                )
            except Exception as exc:  # SMTP failure shouldn't fail the invite — the link still works
                out["emailError"] = str(exc)
        out["emailed"] = emailed
    return out


@router.put("/members/{user_id}")
async def update_member_role(user_id: str, body: RoleUpdateIn, claims: dict = Depends(_require_app_admin), session: AsyncSession = Depends(get_session)):
    app = await _app(session, claims["aud"])
    membership = await session.scalar(
        select(Membership)
        .where(Membership.user_id == user_id, Membership.org_id == claims["org"], Membership.app_id == app.id)
        .options(selectinload(Membership.roles))
    )
    if not membership:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    membership.roles = [await _ensure_role(session, app, claims["org"], body.role)]
    await session.commit()
    await session.refresh(membership, ["roles"])
    user = await session.get(User, user_id)
    return _member_dict(user, membership)


@router.delete("/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(user_id: str, claims: dict = Depends(_require_app_admin), session: AsyncSession = Depends(get_session)):
    if user_id == claims.get("sub"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "you cannot remove yourself")
    app = await _app(session, claims["aud"])
    membership = await session.scalar(
        select(Membership).where(
            Membership.user_id == user_id, Membership.org_id == claims["org"], Membership.app_id == app.id
        )
    )
    if membership:
        await session.delete(membership)
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
