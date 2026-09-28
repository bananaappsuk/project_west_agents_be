"""Org-scoped membership view for an application.

Scoped to the caller's own org + app (taken from their token) and requires the
app-admin scope (`{app}:admin`). This powers the "Users & Roles" screen, which is
read-only: people and their roles are managed in Microsoft Entra ID (app roles on
the PW Agents enterprise app) and synced into memberships on every sign-in (see
routers/entra.py). The only write left is removing a *legacy* member — a pre-Entra
account never linked to an Entra user, which can no longer sign in anyway.

Role labels map to internal role names + permission sets so each app's service RBAC
keeps working:
    Superadmin -> owner  ({app}:admin, {app}:agent.invoke)   [full access via admin bypass]
    Admin      -> admin  (read + write + agent.invoke)
    User       -> user   (read only)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_session
from ..deps import current_claims
from ..models import Application, Membership, Role, User
from .auth import _ensure_permissions

router = APIRouter(prefix="/orgs", tags=["orgs"])

DISPLAY_TO_INTERNAL = {"Superadmin": "owner", "Admin": "admin", "User": "user"}
INTERNAL_TO_DISPLAY = {"owner": "Superadmin", "admin": "Admin", "user": "User"}


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
        # "entra": role comes from Entra app roles, read-only here. "legacy": a pre-Entra
        # account that was never linked to an Entra user; it can't sign in.
        "managedBy": "entra" if user.entra_oid else "legacy",
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


@router.delete("/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_legacy_member(user_id: str, claims: dict = Depends(_require_app_admin), session: AsyncSession = Depends(get_session)):
    if user_id == claims.get("sub"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "you cannot remove yourself")
    user = await session.get(User, user_id)
    if user and user.entra_oid:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this user is managed in Microsoft Entra ID — remove their PW Agents assignment there",
        )
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
