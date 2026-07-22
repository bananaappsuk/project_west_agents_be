"""Shared common APIs (usable by all applications).

Placeholder for cross-cutting services (files, notifications, knowledge-base
management). For now it exposes an authenticated identity echo so callers can
confirm token verification end-to-end.
"""

from fastapi import APIRouter, Depends

from ..deps import current_claims

router = APIRouter(prefix="/common", tags=["common"])


@router.get("/whoami")
async def whoami(claims: dict = Depends(current_claims)):
    return {
        "sub": claims.get("sub"),
        "typ": claims.get("typ"),
        "org": claims.get("org"),
        "app": claims.get("aud"),
        "scope": claims.get("scope"),
    }
