from fastapi import Depends, HTTPException, status

from platform_common import Auth

from .config import settings

# audience = app_key: only west-agent tokens are accepted here.
auth = Auth(jwks_url=settings.auth_jwks_url, issuer=settings.auth_issuer, audience=settings.app_key)


def require(*needed: str):
    """Allow if the token holds any of `needed` — or the app admin scope."""

    async def dependency(claims: dict = Depends(auth.claims)) -> dict:
        granted = set((claims.get("scope") or "").split())
        if f"{settings.app_key}:admin" in granted or (set(needed) & granted):
            return claims
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing scope: {' or '.join(needed)}")

    return dependency
