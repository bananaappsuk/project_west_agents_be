"""Microsoft Entra ID sign-in: exchange an Entra access token for platform tokens.

The frontend signs the user in with MSAL and POSTs the access token it got for
`api://<client-id>/access_as_user` as a Bearer header. We verify it (app/entra.py),
find the user by Entra object id (`oid`), sync their Entra app role into
their memberships for every agent app, and return platform tokens for each app —
so the gateway and every downstream service keep verifying our own JWTs unchanged.

Entra is the source of truth for access:
- the enterprise app has "Assignment required", so only assigned staff get a token;
- the `roles` claim (Agents.Superadmin / Agents.Admin / Agents.User) sets the role,
  re-synced on every sign-in; no role → no access (never a default role);
- a pre-Entra user row is bound to its Entra account on first sign-in (UPN = email,
  our tenant, row not already bound) — from then on it's found by `oid`;
- an assigned user with no local row is provisioned on first sign-in.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import get_session
from ..entra import EntraTokenError, verify_access_token
from ..models import Application, Membership, Organization, RefreshToken, Role, User
from ..schemas import EntraTokensOut, TokenOut
from .auth import _issue_tokens, _token_fields
from .orgs import _ensure_role

log = logging.getLogger("auth.entra")

router = APIRouter(prefix="/auth", tags=["auth"])

# Entra app role value -> the display role routers/orgs.py maps to permissions.
# Highest wins if a user somehow holds more than one.
ENTRA_ROLES = [("Agents.Superadmin", "Superadmin"), ("Agents.Admin", "Admin"), ("Agents.User", "User")]

# Every staff member gets the same role in both agents (as the old invite flow did).
AGENT_APPS = ["mail-agent", "voice-agent"]

# Returned as the 403 `detail` so the frontend can show its "no access" page.
NO_ACCESS = "no_access"


def _display_role(entra_roles: list[str]) -> str | None:
    for value, display in ENTRA_ROLES:
        if value in entra_roles:
            return display
    return None


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    return authorization.split(" ", 1)[1].strip()


async def _revoke_access(session: AsyncSession, user: User) -> None:
    """Role removed in Entra: drop the user's memberships and end their sessions now,
    rather than letting existing refresh tokens run to their expiry."""
    for membership in await session.scalars(select(Membership).where(Membership.user_id == user.id)):
        await session.delete(membership)
    await session.execute(
        update(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False)).values(revoked=True)
    )
    await session.commit()


async def _bind_existing(session: AsyncSession, claims: dict, oid: str, upn: str) -> User | None:
    """Bind-on-first-sign-in for pre-Entra accounts: a users row whose email equals
    the token's UPN (case-insensitive) and has no entra_oid yet gets this oid.
    Returns None when no row matches (caller provisions). Every bind is logged."""
    rows = list(await session.scalars(select(User).where(func.lower(User.email) == upn.lower())))
    if not rows:
        return None
    if len(rows) > 1:
        log.warning("AUDIT entra bind refused: %d users share email %s (token oid %s)", len(rows), upn, oid)
        raise HTTPException(status.HTTP_409_CONFLICT, "More than one account uses this email. Ask an administrator.")
    user = rows[0]
    if user.entra_oid:
        # Found by email but not by oid → already bound to a different Microsoft account.
        log.warning(
            "AUDIT entra bind refused: user %s (%s) is bound to oid %s, token oid %s",
            user.id, user.email, user.entra_oid, oid,
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This account is already linked to a different Microsoft account. Ask an administrator.",
        )
    # Already enforced by verify_access_token; re-checked here because a bind is permanent.
    if claims.get("tid") != settings.entra_tenant_id:
        log.warning("AUDIT entra bind refused: user %s (%s), token tid %s", user.id, user.email, claims.get("tid"))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token is from a different tenant")

    user.entra_oid = oid
    user.entra_upn = upn
    log.info(
        "AUDIT entra bind: user %s (%s) bound to oid %s upn %s tid %s",
        user.id, user.email, oid, upn, claims["tid"],
    )
    return user


async def _find_or_provision_user(session: AsyncSession, claims: dict) -> User:
    oid = claims["oid"]
    upn = claims.get("upn") or claims.get("preferred_username")
    email = (claims.get("email") or upn or "").lower() or None
    name = claims.get("name")

    user = await session.scalar(select(User).where(User.entra_oid == oid))
    if user is None and upn:
        user = await _bind_existing(session, claims, oid, upn)
    if user is None:
        if not email:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Microsoft token carries no email or UPN")
        # The UPN matched no row, but the email claim does (UPN ≠ email): not a bind
        # rule we accept — refuse rather than guess.
        if await session.scalar(select(User).where(func.lower(User.email) == email)):
            log.warning("AUDIT entra sign-in refused: %s exists but doesn't match UPN %s (oid %s)", email, upn, oid)
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "An existing account uses this email but doesn't match your Microsoft sign-in name. "
                "Ask an administrator.",
            )
        user = User(email=email, full_name=name, entra_oid=oid, entra_upn=upn, is_active=True)
        session.add(user)
        await session.flush()
        log.info("AUDIT entra sign-in: provisioned %s (oid %s)", email, oid)
        return user

    # Keep profile fields in step with Entra.
    user.entra_upn = upn or user.entra_upn
    if name:
        user.full_name = name
    if email and email != user.email.lower() and not await session.scalar(
        select(User.id).where(func.lower(User.email) == email)
    ):
        user.email = email
    user.is_active = True
    return user


async def _app_for(session: AsyncSession, key: str) -> Application:
    app = await session.scalar(select(Application).where(Application.key == key))
    if app is None:
        app = Application(key=key, name=key)
        session.add(app)
        await session.flush()
    return app


def _entra_groups(claims: dict) -> list[str]:
    """Entra security-group object ids from the signed-in user's token.

    These are passed through to the agent access tokens so the mail agent can
    resolve which shared mailboxes the person may read and send from. Only raw
    group ids travel; the resolved mailbox set is never put in a token.

    Entra replaces the `groups` claim with `_claim_names`/`_claim_sources` once a
    user is in roughly 200+ groups. That is unlikely at Project West's size, but it
    is logged rather than ignored: silently treating an overage as "no groups" would
    revoke everyone's mailbox access at once, and would look to the user like an
    empty inbox rather than an error.
    """
    raw = claims.get("groups")
    if isinstance(raw, (list, tuple)):
        log.info("AUDIT entra groups oid=%s count=%d", claims.get("oid"), len(raw))
        return [str(g) for g in raw if g]

    # Neither a groups claim nor an overage indicator. Logged with the claim names
    # present because the two causes are indistinguishable downstream: the app
    # registration is not emitting the claim, or the caller presented a token
    # minted before it was turned on. Claim NAMES only -- never their values.
    log.warning("AUDIT entra NO groups claim oid=%s claims_present=%s",
                claims.get("oid"), sorted(claims.keys()))
    if "_claim_names" in claims or "_claim_sources" in claims:
        log.warning(
            "AUDIT entra groups overage for oid %s - group claim omitted by Entra; "
            "mailbox access cannot be resolved from this token",
            claims.get("oid"),
        )
    return []


@router.post("/entra", response_model=EntraTokensOut)
async def entra_sign_in(authorization: str | None = Header(default=None), session: AsyncSession = Depends(get_session)):
    try:
        claims = await verify_access_token(_bearer(authorization))
    except EntraTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    role = _display_role(claims.get("roles") or [])
    if role is None:
        existing = await session.scalar(select(User).where(User.entra_oid == claims["oid"]))
        if existing:
            await _revoke_access(session, existing)
        raise HTTPException(status.HTTP_403_FORBIDDEN, NO_ACCESS)

    org = await session.scalar(select(Organization))
    if org is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "organization not set up — run scripts/seed_org.py")

    user = await _find_or_provision_user(session, claims)
    groups = _entra_groups(claims)
    # Persisted so a token REFRESH can reproduce them -- see User.entra_groups.
    # Written every sign-in, so a group removal in Entra lands here too.
    user.entra_groups = groups

    tokens: dict[str, TokenOut] = {}
    for app_key in AGENT_APPS:
        app = await _app_for(session, app_key)
        app_role = await _ensure_role(session, app, org.id, role)
        membership = await session.scalar(
            select(Membership)
            .where(Membership.user_id == user.id, Membership.org_id == org.id, Membership.app_id == app.id)
            .options(selectinload(Membership.roles).selectinload(Role.permissions))
        )
        if membership is None:
            membership = Membership(user_id=user.id, org_id=org.id, app_id=app.id, roles=[app_role])
            session.add(membership)
        else:
            membership.roles = [app_role]
        roles, scope = _token_fields(app.key, membership)
        tokens[app_key] = await _issue_tokens(
            session, user=user, org_id=org.id, app=app, roles=roles, scope=scope,
            groups=groups, commit=False
        )

    await session.commit()
    return EntraTokensOut(tokens=tokens)
