"""Microsoft Entra ID access-token verification for POST /auth/entra.

The frontend signs the user in with MSAL and sends the *access* token it got for
this API (`api://<client-id>/access_as_user`). We accept it only if it is a v2
token from our tenant, for our client id, carries the delegated scope, and was
issued within the last few minutes — a fresh sign-in, not a replayed old token.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
from fastapi.concurrency import run_in_threadpool

from .config import settings


class EntraTokenError(Exception):
    """The token is missing, malformed, or fails a check. Message is safe to return."""


_jwks_client: jwt.PyJWKClient | None = None

# Entra backdates iat/nbf by 5 minutes for clock skew; the real issue time is iat + 300.
_ENTRA_IAT_BACKDATE = 300


def _authority() -> str:
    return f"https://login.microsoftonline.com/{settings.entra_tenant_id}"


def _jwks() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        # Tenant-specific keys endpoint; PyJWKClient caches keys and refetches on an unknown kid.
        _jwks_client = jwt.PyJWKClient(f"{_authority()}/discovery/v2.0/keys", cache_keys=True, lifespan=3600)
    return _jwks_client


def _verify(token: str) -> dict[str, Any]:
    if not (settings.entra_tenant_id and settings.entra_client_id):
        raise EntraTokenError("Microsoft sign-in is not configured on the server")
    try:
        signing_key = _jwks().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=settings.entra_client_id,
            # v2 issuer — a v1 token (iss = sts.windows.net/...) fails here.
            issuer=f"{_authority()}/v2.0",
            leeway=60,
            options={"require": ["exp", "nbf", "iat", "oid", "tid", "scp"]},
        )
    except jwt.PyJWTError as exc:
        raise EntraTokenError(f"invalid Microsoft token: {exc}") from exc

    if claims["tid"] != settings.entra_tenant_id:
        raise EntraTokenError("token is from a different tenant")
    if settings.entra_scope not in str(claims["scp"]).split():
        raise EntraTokenError(f"token is missing the {settings.entra_scope} scope")
    # Measured from when Entra actually issued the token, not the backdated iat.
    if claims["iat"] + _ENTRA_IAT_BACKDATE < time.time() - settings.entra_max_token_age:
        raise EntraTokenError("token is too old — sign in again")
    return claims


async def verify_access_token(token: str) -> dict[str, Any]:
    # PyJWKClient fetches keys with blocking urllib, so keep it off the event loop.
    return await run_in_threadpool(_verify, token)
