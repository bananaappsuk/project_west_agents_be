"""RBAC administration. All routes require the `platform:admin` scope."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..deps import require_platform_admin
from ..models import Application, Membership, Organization, Permission, Role, User
from ..schemas import ApplicationIn, MemberIn, PermissionIn, ProvisionIn, RoleIn
from ..security import hash_password
from .auth import _ensure_permissions, _slug

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_platform_admin)])


@router.get("/applications")
async def list_applications(session: AsyncSession = Depends(get_session)):
    rows = await session.scalars(select(Application))
    return [{"id": a.id, "key": a.key, "name": a.name} for a in rows]


@router.post("/applications", status_code=status.HTTP_201_CREATED)
async def create_application(body: ApplicationIn, session: AsyncSession = Depends(get_session)):
    if await session.scalar(select(Application).where(Application.key == body.key)):
        raise HTTPException(status.HTTP_409_CONFLICT, "application key already exists")
    app = Application(key=body.key, name=body.name)
    session.add(app)
    await session.commit()
    return {"id": app.id, "key": app.key, "name": app.name}


@router.post("/permissions", status_code=status.HTTP_201_CREATED)
async def create_permission(body: PermissionIn, session: AsyncSession = Depends(get_session)):
    if await session.scalar(select(Permission).where(Permission.key == body.key)):
        raise HTTPException(status.HTTP_409_CONFLICT, "permission already exists")
    app_id = None
    if body.app_key:
        app = await session.scalar(select(Application).where(Application.key == body.app_key))
        if not app:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "application not found")
        app_id = app.id
    perm = Permission(key=body.key, app_id=app_id, description=body.description)
    session.add(perm)
    await session.commit()
    return {"id": perm.id, "key": perm.key}


@router.post("/roles", status_code=status.HTTP_201_CREATED)
async def create_role(body: RoleIn, session: AsyncSession = Depends(get_session)):
    app = await session.scalar(select(Application).where(Application.key == body.app_key))
    if not app:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "application not found")
    perms = list(await session.scalars(select(Permission).where(Permission.key.in_(body.permission_keys))))
    role = Role(name=body.name, app_id=app.id, org_id=body.org_id, permissions=perms)
    session.add(role)
    await session.commit()
    return {"id": role.id, "name": role.name, "permissions": [p.key for p in perms]}


@router.post("/members", status_code=status.HTTP_201_CREATED)
async def add_member(body: MemberIn, session: AsyncSession = Depends(get_session)):
    app = await session.scalar(select(Application).where(Application.key == body.app_key))
    if not app:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "application not found")

    user = await session.scalar(select(User).where(User.email == body.email))
    if not user:
        if not body.password:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "password required to create a new user")
        user = User(email=body.email, password_hash=hash_password(body.password), full_name=body.full_name)
        session.add(user)
        await session.flush()

    roles = list(
        await session.scalars(
            select(Role).where(Role.app_id == app.id, Role.name.in_(body.role_names))
        )
    )
    membership = await session.scalar(
        select(Membership).where(
            Membership.user_id == user.id, Membership.org_id == body.org_id, Membership.app_id == app.id
        )
    )
    if not membership:
        membership = Membership(user_id=user.id, org_id=body.org_id, app_id=app.id, roles=roles)
        session.add(membership)
    else:
        membership.roles = roles
    await session.commit()
    return {"membership_id": membership.id, "roles": [r.name for r in roles]}


@router.post("/provision", status_code=status.HTTP_201_CREATED)
async def provision_org(body: ProvisionIn, session: AsyncSession = Depends(get_session)):
    """Create a new org + owner membership for an application in one call.

    This is the "sell one app to an org" operation: the org gets access to exactly
    this app (owner role, app-scoped permissions) and nothing else. The billing
    Subscription for the org is created separately in the Billing service.
    """
    app = await session.scalar(select(Application).where(Application.key == body.app_key))
    if app is None:
        app = Application(key=body.app_key, name=body.app_key)
        session.add(app)
        await session.flush()

    keys = body.permission_keys or [
        f"{app.key}:admin",
        f"{app.key}:agent.invoke",
        f"{app.key}:emails.read",
        f"{app.key}:emails.write",
    ]
    perms = await _ensure_permissions(session, app, keys)

    org = Organization(name=body.org_name, slug=_slug(body.org_name))
    session.add(org)
    await session.flush()

    role = Role(name="owner", app_id=app.id, org_id=org.id, permissions=perms)
    session.add(role)
    await session.flush()

    user = await session.scalar(select(User).where(User.email == body.email))
    if not user:
        if not body.password:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "password required to create a new user")
        user = User(email=body.email, password_hash=hash_password(body.password), full_name=body.full_name)
        session.add(user)
        await session.flush()

    membership = Membership(user_id=user.id, org_id=org.id, app_id=app.id, roles=[role])
    session.add(membership)
    await session.commit()
    return {
        "org_id": org.id,
        "user_id": user.id,
        "membership_id": membership.id,
        "app_key": app.key,
        "roles": ["owner"],
        "scope": sorted(p.key for p in perms),
    }
