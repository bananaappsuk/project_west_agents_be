"""RBAC administration. All routes require the `platform:admin` scope.

Members aren't added here: people and their roles come from Microsoft Entra ID
(routers/entra.py)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..deps import require_platform_admin
from ..models import Application, Permission, Role
from ..schemas import ApplicationIn, PermissionIn, RoleIn

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

