from __future__ import annotations

from pydantic import BaseModel


# ---- auth ----
class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str


class RefreshIn(BaseModel):
    refresh_token: str


class LogoutIn(BaseModel):
    refresh_token: str


class EntraTokensOut(BaseModel):
    """Platform tokens for every agent app, keyed by app key ("mail-agent", "voice-agent")."""
    tokens: dict[str, TokenOut]


# ---- admin / RBAC ----
class ApplicationIn(BaseModel):
    key: str
    name: str


class PermissionIn(BaseModel):
    key: str
    app_key: str | None = None   # None -> platform-level permission
    description: str | None = None


class RoleIn(BaseModel):
    name: str
    app_key: str
    org_id: str | None = None
    permission_keys: list[str] = []
