"""JWT verification + scope-enforcement dependencies for FastAPI services.

A service configures one `Auth` instance pointed at the Auth service's JWKS URL,
then uses `auth.claims` to require a valid token and `auth.require_scope(...)` to
require specific permissions on a route.
"""

from __future__ import annotations

from typing import Any

import jwt
from fastapi import Depends, Header, HTTPException, status
from jwt import PyJWKClient


class Auth:
    def __init__(self, jwks_url: str, issuer: str, audience: str | None = None):
        """
        jwks_url : the Auth service's `/.well-known/jwks.json`
        issuer   : expected `iss` claim
        audience : if set, the token's `aud` must equal it (an app key, e.g. "west-agent")
        """
        self.jwks_url = jwks_url
        self.issuer = issuer
        self.audience = audience
        self._jwk_client = PyJWKClient(jwks_url)

    def _decode(self, token: str) -> dict[str, Any]:
        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token).key
            return jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={
                    "require": ["exp", "iat", "sub"],
                    "verify_aud": self.audience is not None,
                },
            )
        except jwt.PyJWTError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}") from exc

    async def claims(self, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """FastAPI dependency: require and return the decoded token claims."""
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        return self._decode(token)

    async def current_org(self, authorization: str | None = Header(default=None)) -> str:
        """FastAPI dependency: the tenant id from the token. Every tenant-scoped
        query in an app service should filter by this value."""
        claims = await self.claims(authorization)
        org = claims.get("org")
        if not org:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "token carries no org")
        return org

    def require_scope(self, *needed: str):
        """FastAPI dependency factory: require the token to carry all given scopes."""

        async def dependency(claims: dict[str, Any] = Depends(self.claims)) -> dict[str, Any]:
            granted = set((claims.get("scope") or "").split())
            missing = set(needed) - granted
            if missing:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    f"missing scope(s): {', '.join(sorted(missing))}",
                )
            return claims

        return dependency
