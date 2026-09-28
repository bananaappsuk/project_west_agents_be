from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import get_session
from ..deps import current_claims
from ..models import Application, Membership, Organization, Permission, RefreshToken, Role, User
from ..schemas import LogoutIn, RefreshIn, TokenOut
from ..security import create_access_token, hash_token, new_refresh_token

# Sign-in is Microsoft Entra ID only (routers/entra.py's POST /auth/entra). This router
# keeps the session endpoints that work on platform tokens: refresh, logout, me.
router = APIRouter(prefix="/auth", tags=["auth"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _slug(name: str) -> str:
    base = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
    return f"{base or 'org'}-{uuid.uuid4().hex[:6]}"


def _token_fields(app_key: str, membership: Membership) -> tuple[list[str], str]:
    roles = [f"{app_key}:{r.name}" for r in membership.roles]
    scope = " ".join(sorted({p.key for r in membership.roles for p in r.permissions}))
    return roles, scope


async def _issue_tokens(
    session: AsyncSession,
    *,
    user: User,
    org_id: str,
    app: Application,
    roles: list[str],
    scope: str,
    expires_at: datetime | None = None,
    commit: bool = True,
) -> TokenOut:
    """Mint an access + refresh token pair. `expires_at` pins the refresh token's
    expiry (a rotation passes its parent's, so the session never outlives
    REFRESH_TOKEN_TTL from sign-in); default is a fresh REFRESH_TOKEN_TTL."""
    access = create_access_token(
        sub=user.id, typ="user", org=org_id, aud=app.key, roles=roles, scope=scope
    )
    raw, token_hash = new_refresh_token()
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=token_hash,
            org_id=org_id,
            app_id=app.id,
            expires_at=expires_at or _utcnow() + timedelta(seconds=settings.refresh_token_ttl),
        )
    )
    if commit:
        await session.commit()
    return TokenOut(access_token=access, expires_in=settings.access_token_ttl, refresh_token=raw)


async def _ensure_permissions(session: AsyncSession, app: Application, keys: list[str]) -> list[Permission]:
    out: list[Permission] = []
    for key in keys:
        perm = await session.scalar(select(Permission).where(Permission.key == key))
        if perm is None:
            perm = Permission(key=key, app_id=None if key.startswith("platform:") else app.id)
            session.add(perm)
            await session.flush()
        out.append(perm)
    return out


@router.post("/refresh", response_model=TokenOut)
async def refresh(body: RefreshIn, session: AsyncSession = Depends(get_session)):
    rt = await session.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(body.refresh_token)))
    if not rt or rt.revoked or rt.expires_at <= _utcnow():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired refresh token")

    user = await session.get(User, rt.user_id)
    app = await session.get(Application, rt.app_id)
    if not user or not app or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid refresh token")

    membership = await session.scalar(
        select(Membership)
        .where(
            Membership.user_id == rt.user_id,
            Membership.app_id == rt.app_id,
            Membership.org_id == rt.org_id,
        )
        .options(selectinload(Membership.roles).selectinload(Role.permissions))
    )
    if not membership:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "membership revoked")

    rt.revoked = True  # rotate: single-use refresh tokens
    roles, scope = _token_fields(app.key, membership)
    return await _issue_tokens(
        session, user=user, org_id=rt.org_id, app=app, roles=roles, scope=scope, expires_at=rt.expires_at
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: LogoutIn, session: AsyncSession = Depends(get_session)):
    rt = await session.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(body.refresh_token)))
    if rt and not rt.revoked:
        rt.revoked = True
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me")
async def me(claims: dict = Depends(current_claims), session: AsyncSession = Depends(get_session)):
    user = await session.get(User, claims["sub"])
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    org = await session.get(Organization, claims["org"]) if claims.get("org") else None
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "org": claims.get("org"),
        "org_name": org.name if org else None,
        "app": claims.get("aud"),
        "roles": claims.get("roles"),
        "scope": claims.get("scope"),
    }
