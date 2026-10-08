from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from platform_common import Auth

from . import access_resolver
from .config import settings
from .db import get_session
from .mailbox_scope import MailboxScope

# audience = app_key: only mail-agent tokens are accepted here.
auth = Auth(jwks_url=settings.auth_jwks_url, issuer=settings.auth_issuer, audience=settings.app_key)


def require(*needed: str):
    """Allow if the token holds any of `needed` — or the app admin scope."""

    async def dependency(claims: dict = Depends(auth.claims)) -> dict:
        granted = set((claims.get("scope") or "").split())
        if f"{settings.app_key}:admin" in granted or (set(needed) & granted):
            return claims
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing scope: {' or '.join(needed)}")

    return dependency


async def mailbox_scope(
    claims: dict = Depends(auth.claims),
    session: AsyncSession = Depends(get_session),
) -> MailboxScope:
    """The authorised mailbox set for the signed-in user.

    NEVER derived from anything in the request body or query string. The only
    inputs are the verified token's subject and its Entra group claim.

    Resolution happens per request against the CURRENT rules in the database rather
    than being baked into the token: an administrator revoking a group's access to a
    mailbox then takes effect on the next request, not whenever that user's access
    token happens to expire. Group membership changes still carry the token's own
    lag, which is the shortest window the sign-in flow allows.

    A user with no groups resolves to an empty scope rather than an error here --
    the route decides how to say "you are authorised for nothing", and it must say
    it explicitly rather than rendering an empty inbox that reads as "no mail today".
    """
    user_id = claims.get("sub") or ""
    raw = claims.get("groups")
    if isinstance(raw, (list, tuple)):
        group_ids = [str(g) for g in raw if g]
    elif "_claim_names" in claims or "_claim_sources" in claims:
        # Entra dropped the claim for group overage. Failing closed is correct;
        # failing SILENTLY is not -- an empty set here is indistinguishable from
        # "member of nothing" and would revoke everyone at once without a trace.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "group membership unavailable for this token; mailbox access cannot be resolved",
        )
    else:
        group_ids = []

    return await access_resolver.resolve_and_store(
        session, user_id=user_id, group_ids=group_ids
    )
