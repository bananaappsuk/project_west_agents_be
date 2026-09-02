from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import get_session
from ..deps import current_claims
from ..mailer import send_password_reset_email
from ..models import Application, Invite, Membership, Organization, PasswordReset, Permission, RefreshToken, Role, User
from ..schemas import (
    AcceptInviteIn,
    ForgotPasswordIn,
    LoginIn,
    LogoutIn,
    RefreshIn,
    ResetPasswordIn,
    TokenOut,
)
from ..security import (
    create_access_token,
    hash_password,
    hash_token,
    new_refresh_token,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

RESET_TTL_HOURS = 2


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
    session: AsyncSession, *, user: User, org_id: str, app: Application, roles: list[str], scope: str
) -> TokenOut:
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
            expires_at=_utcnow() + timedelta(seconds=settings.refresh_token_ttl),
        )
    )
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


async def _provision_owner_membership(
    session: AsyncSession, *, user: User, org: Organization, app_key: str
) -> tuple[Application, Membership]:
    """Find-or-create `app_key` and grant `user` an Owner membership in `org` for it.

    No longer called from this router — self-serve registration (`/auth/register`)
    was removed when the product moved to a single, seeded organization (see
    scripts/seed_org.py, the only remaining caller, which reuses this to provision
    the seeded admin's Owner access to both apps)."""
    app = await session.scalar(select(Application).where(Application.key == app_key))
    if app is None:
        app = Application(key=app_key, name=app_key)
        session.add(app)
        await session.flush()

    perms = await _ensure_permissions(
        session, app, ["platform:admin", f"{app.key}:admin", f"{app.key}:agent.invoke"]
    )
    role = Role(name="owner", app_id=app.id, org_id=org.id, permissions=perms)
    session.add(role)
    await session.flush()
    membership = Membership(user_id=user.id, org_id=org.id, app_id=app.id, roles=[role])
    session.add(membership)
    await session.flush()
    return app, membership


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, session: AsyncSession = Depends(get_session)):
    user = await session.scalar(select(User).where(User.email == body.email))
    if not user or not user.is_active or not verify_password(user.password_hash, body.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    app = await session.scalar(select(Application).where(Application.key == body.app_key))
    if app is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "application not registered")

    query = (
        select(Membership)
        .where(Membership.user_id == user.id, Membership.app_id == app.id)
        .options(selectinload(Membership.roles).selectinload(Role.permissions))
    )
    if body.org_id:
        query = query.where(Membership.org_id == body.org_id)
    memberships = list(await session.scalars(query))

    if not memberships:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to this application")
    if len(memberships) > 1:
        orgs = [m.org_id for m in memberships]
        raise HTTPException(status.HTTP_409_CONFLICT, f"multiple orgs; specify org_id: {orgs}")

    membership = memberships[0]
    roles, scope = _token_fields(app.key, membership)
    return await _issue_tokens(session, user=user, org_id=membership.org_id, app=app, roles=roles, scope=scope)


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
    return await _issue_tokens(session, user=user, org_id=rt.org_id, app=app, roles=roles, scope=scope)


@router.post("/accept-invite", response_model=TokenOut)
async def accept_invite(body: AcceptInviteIn, session: AsyncSession = Depends(get_session)):
    """Public: an invited teammate sets their password and is signed in.

    Verifies the one-time invite token, activates the user, and issues tokens for the
    org + app the invite was for.
    """
    inv = await session.scalar(select(Invite).where(Invite.token_hash == hash_token(body.token)))
    if not inv or inv.accepted or inv.expires_at <= _utcnow():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired invite")

    user = await session.get(User, inv.user_id)
    app = await session.get(Application, inv.app_id)
    if not user or not app:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite target not found")

    user.password_hash = hash_password(body.password)
    if body.full_name:
        user.full_name = body.full_name
    user.is_active = True
    inv.accepted = True

    membership = await session.scalar(
        select(Membership)
        .where(Membership.user_id == user.id, Membership.org_id == inv.org_id, Membership.app_id == inv.app_id)
        .options(selectinload(Membership.roles).selectinload(Role.permissions))
    )
    if not membership:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "membership was revoked")

    roles, scope = _token_fields(app.key, membership)
    return await _issue_tokens(session, user=user, org_id=inv.org_id, app=app, roles=roles, scope=scope)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: LogoutIn, session: AsyncSession = Depends(get_session)):
    rt = await session.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(body.refresh_token)))
    if rt and not rt.revoked:
        rt.revoked = True
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/forgot-password", status_code=status.HTTP_204_NO_CONTENT)
async def forgot_password(body: ForgotPasswordIn, session: AsyncSession = Depends(get_session)):
    """Public: request a password-reset link. Always returns 204 whether or not the
    email is registered, so the response can't be used to enumerate accounts."""
    user = await session.scalar(select(User).where(User.email == body.email))
    if user and user.is_active:
        raw = secrets.token_urlsafe(32)
        session.add(PasswordReset(
            user_id=user.id, token_hash=hash_token(raw),
            expires_at=_utcnow() + timedelta(hours=RESET_TTL_HOURS),
        ))
        await session.commit()
        if body.reset_base:
            link = f"{body.reset_base.rstrip('/')}/reset-password?token={raw}"
            try:
                await run_in_threadpool(send_password_reset_email, to=user.email, reset_link=link)
            except Exception:
                pass  # SMTP failure shouldn't leak through — the link still exists
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(body: ResetPasswordIn, session: AsyncSession = Depends(get_session)):
    """Public: consume a one-time reset token and set a new password. Revokes all of
    the user's existing refresh tokens so a stolen session doesn't survive the reset."""
    pr = await session.scalar(select(PasswordReset).where(PasswordReset.token_hash == hash_token(body.token)))
    if not pr or pr.used or pr.expires_at <= _utcnow():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired reset link")

    user = await session.get(User, pr.user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")

    user.password_hash = hash_password(body.password)
    pr.used = True
    await session.execute(update(RefreshToken).where(RefreshToken.user_id == user.id).values(revoked=True))
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
