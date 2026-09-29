"""Role definitions for the agent apps: display role -> internal role + permissions.

Not a router any more — people and roles are managed in Microsoft Entra ID and
synced into memberships on every sign-in (routers/entra.py uses `_ensure_role`).
Kept under this name because scripts/backfill_companion_access.py imports from it.

Role labels map to internal role names + permission sets so each app's service RBAC
keeps working:
    Superadmin -> owner  ({app}:admin, {app}:agent.invoke)   [full access via admin bypass]
    Admin      -> admin  (read + write + agent.invoke)
    User       -> user   (read only)
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Application, Role
from .auth import _ensure_permissions

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
