"""Local token verification for the auth service's own protected routes.

Other services verify via JWKS (platform_common.Auth); the auth service can verify
its own tokens directly with the public key it holds.
"""

from __future__ import annotations

import jwt
from fastapi import Depends, Header, HTTPException, status

from .config import settings
from .keys import public_key


def current_claims(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        return jwt.decode(
            token,
            public_key(),
            algorithms=["RS256"],
            issuer=settings.auth_issuer,
            options={"verify_aud": False, "require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}") from exc


def require_platform_admin(claims: dict = Depends(current_claims)) -> dict:
    if "platform:admin" not in (claims.get("scope") or "").split():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "requires platform:admin")
    return claims
