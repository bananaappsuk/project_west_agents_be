"""Microsoft Entra ID sign-in: exchange an Entra access token for platform tokens.

The frontend signs the user in with MSAL and POSTs the access token it got for
`api://<client-id>/access_as_user` as a Bearer header. We verify it (app/entra.py),
find the user by Entra object id (`oid`), work out which agent apps they are
entitled to, and return platform tokens for **those apps only** — so the gateway
and every downstream service keep verifying our own JWTs unchanged.

Entra is the source of truth for access:
- the enterprise app has "Assignment required", so only assigned staff get a token;
- the `roles` claim (Agents.Superadmin / Agents.Admin / Agents.User) sets the role,
  re-synced on every sign-in; no role → no access (never a default role);
- a pre-Entra user row is bound to its Entra account on first sign-in (UPN = email,
  our tenant, row not already bound) — from then on it's found by `oid`;
- an assigned user with no local row is provisioned on first sign-in.

Entitlement is per application, and the two agents do not share a rule
(`_entitled_apps`):

- **Email Agent** — the app role is necessary and sufficient, unchanged. Which
  shared mailboxes the person may then read, send from or approve in is decided by
  the mail agent against its own access rules, not here.
- **Voice Agent** — the app role *plus* membership of the `PW-Voice-Users` Entra
  security group (`ENTRA_VOICE_GROUP_ID`). Voice has no internal roles and no
  per-recording permissions, so that group is the whole of its access model: a
  member gets the entire capability, a non-member gets no Voice token at all.

A membership is created only for an entitled app, and an existing membership for an
app the user is no longer entitled to is removed along with that app's refresh
tokens (`_revoke_app_access`). Before this, the loop provisioned a membership for
every agent app on every sign-in, so everyone who could sign in received a Voice
token automatically — which is what this endpoint is being changed to stop.
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

MAIL_APP = "mail-agent"
VOICE_APP = "voice-agent"

#: The agent apps a sign-in is *considered* for. Which of them it is actually
#: entitled to is `_entitled_apps`; this list only fixes the order so the audit
#: line and the response are stable.
AGENT_APPS = [MAIL_APP, VOICE_APP]

# Returned as the 403 `detail` so the frontend can show its "no access" page.
NO_ACCESS = "no_access"


def _display_role(entra_roles: list[str]) -> str | None:
    for value, display in ENTRA_ROLES:
        if value in entra_roles:
            return display
    return None


def _entitled_apps(*, role: str | None, groups: list[str], oid: str | None = None) -> tuple[str, ...]:
    """Which agent apps this sign-in may be issued tokens for.

    Pure: no I/O, no database, no session. The one decision that governs whether a
    person can reach a capability at all is therefore also the cheapest thing in
    this service to test exhaustively -- the same reason the mail agent keeps
    `receiving_address.resolve` free of I/O.

    **Email is unchanged.** An Entra app role is necessary and sufficient, exactly
    as it was before this function existed. Mailbox-level authorization continues to
    happen in the mail agent against its own `mailbox_access_rule` rows; nothing here
    touches it.

    **Voice is group-based and all-or-nothing.** Membership of `PW-Voice-Users`
    (`ENTRA_VOICE_GROUP_ID`) is required on top of the app role. There are no
    internal Voice roles and no per-recording permissions, so a member receives the
    whole capability and a non-member receives no token for it -- which makes this
    single decision the entire Voice authorization model, and the reason it is
    stated in one place rather than spread across the Voice service's routes.

    Note that the role is still required for Voice as well as the group. Two
    reasons, neither cosmetic: the PW Agents enterprise app has "Assignment
    required", so a user with no app role cannot obtain an Entra token for this API
    in the first place; and the role is what supplies the membership's permission
    set (`routers/orgs.py::_perm_keys`), so there is no coherent membership to
    create without one. Group membership alone is therefore not a grant.

    Fails closed on both ways this can be misconfigured, because the previous
    behaviour of this endpoint was to hand every signed-in user a Voice token:

      * no `entra_voice_group_id` configured -> nobody is entitled to Voice;
      * no `groups` claim on the token at all -> nobody is entitled to Voice.

    The second case is the Entra group-overage path (`_entra_groups` returns an
    empty list and logs it). Denying Voice there is correct, and it is logged
    distinctly below because "not in the group" and "we could not see any group"
    are indistinguishable downstream while having completely different fixes.
    """
    if role is None:
        return ()

    apps = [MAIL_APP]

    voice_group = (settings.entra_voice_group_id or "").strip()
    if not voice_group:
        log.warning(
            "AUDIT entra voice DENIED oid=%s reason=no_group_configured "
            "(set ENTRA_VOICE_GROUP_ID to the PW-Voice-Users object id)", oid,
        )
    elif not groups:
        log.warning(
            "AUDIT entra voice DENIED oid=%s reason=no_groups_claim "
            "(group overage, or the app registration is not emitting the claim)", oid,
        )
    elif voice_group.lower() in {str(g).strip().lower() for g in groups if g}:
        apps.append(VOICE_APP)
    else:
        log.info("AUDIT entra voice DENIED oid=%s reason=not_in_group", oid)

    return tuple(apps)


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


async def _revoke_app_access(session: AsyncSession, *, user: User, app: Application) -> bool:
    """Entitlement withdrawn for ONE app: drop that membership and end that app's
    sessions. Returns whether anything was actually revoked.

    Narrower than `_revoke_access`, which drops every membership when the Entra app
    role itself is gone. Here the user keeps the other agent entirely.

    This is not belt-and-braces on top of refusing the token -- it is load-bearing.
    `POST /auth/refresh` re-reads the membership row and mints a fresh access token
    whenever one exists (routers/auth.py), so a stale membership plus a refresh
    token still in play would keep renewing Voice access for up to the remainder of
    the 8-hour session. Removing the membership makes the refresh path answer
    "membership revoked" at the next rotation instead, which is at most
    `access_token_ttl` (15 minutes) away.

    Deliberately does NOT commit: the caller commits once for the whole sign-in, so
    a failure part-way through cannot leave a user with a revoked membership and no
    tokens.
    """
    revoked = False
    for membership in await session.scalars(
        select(Membership).where(Membership.user_id == user.id, Membership.app_id == app.id)
    ):
        await session.delete(membership)
        revoked = True

    result = await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == user.id,
            RefreshToken.app_id == app.id,
            RefreshToken.revoked.is_(False),
        )
        .values(revoked=True)
    )
    if result.rowcount:
        revoked = True

    if revoked:
        log.info("AUDIT entra entitlement revoked user=%s app=%s", user.id, app.key)
    return revoked


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

    ── Entra token configuration this depends on ────────────────────────────────
    The token read here is the **access token** for `api://<ENTRA_CLIENT_ID>/
    access_as_user` (see app/entra.py: audience is the client id, issuer is v2).
    Entra does not emit `groups` by default, and it is configured per *token type*,
    so the requirement is specific:

      On the `ENTRA_CLIENT_ID` app registration -> Token configuration ->
      "Add groups claim", with the **Access** token box ticked.
      (Manifest equivalent: `groupMembershipClaims`, plus an `optionalClaims.
      accessToken` entry named `groups`.)

    Ticking only **ID** is the failure that looks like a code bug: sign-in succeeds,
    `oid`/`tid`/`scp` all verify, and `groups` is simply absent -- so Voice is denied
    to everyone and mailbox access resolves to nothing. `_verify` deliberately does
    not require the claim (it is not an authentication input), which is why the
    symptom surfaces here rather than as a 401.

    `groupMembershipClaims` choice is Project West's, and the two sensible values
    differ in a way worth knowing: `SecurityGroup` emits every security group the
    user belongs to, while `ApplicationGroup` emits only groups assigned to this
    enterprise application. `ApplicationGroup` is the safer fit here -- it keeps the
    claim small (so overage below is unreachable in practice) and makes "this group
    participates in authorization" an explicit act rather than a side effect of
    directory membership.

    Object ids are what arrive, which is what both consumers compare against:
    `ENTRA_VOICE_GROUP_ID` here, and `mailbox_access_rule.entra_group_id` in the
    mail agent. Nothing matches on group *name*, so renaming a group is safe.
    ─────────────────────────────────────────────────────────────────────────────

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

    # Which capabilities this person may reach at all. Decided once, from verified
    # token claims only, before any membership is created -- so a membership is a
    # consequence of entitlement rather than the thing that establishes it. That
    # ordering is the fix: this loop used to provision a membership for every app
    # unconditionally, which handed a Voice token to everyone who could sign in.
    entitled = _entitled_apps(role=role, groups=groups, oid=claims.get("oid"))
    log.info(
        "AUDIT entra entitlement oid=%s role=%s groups_in_token=%d apps=%s",
        claims.get("oid"), role, len(groups), ",".join(entitled) or "-",
    )

    tokens: dict[str, TokenOut] = {}
    for app_key in AGENT_APPS:
        app = await _app_for(session, app_key)

        if app_key not in entitled:
            # No token, and no membership left behind for the refresh path to renew
            # from. Nothing is created for an app the user may not have.
            await _revoke_app_access(session, user=user, app=app)
            continue

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

    # Entitled to nothing at all. Reachable only if the entitlement rules change to
    # make the app role insufficient for Email too; today a user with no role has
    # already been refused above. Answered the same way regardless, so the frontend
    # has one no-access path rather than two.
    if not tokens:
        log.warning("AUDIT entra sign-in refused: no entitled app oid=%s", claims.get("oid"))
        raise HTTPException(status.HTTP_403_FORBIDDEN, NO_ACCESS)

    await session.commit()
    return EntraTokensOut(tokens=tokens)
