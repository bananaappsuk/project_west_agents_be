from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


# ---- auth ----
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str | None = None
    org_name: str = Field(min_length=1)   # self-serve signup creates a new org + owner
    app_key: str = Field(min_length=1)     # which application the org signs up for


class LoginIn(BaseModel):
    email: EmailStr
    password: str
    app_key: str
    org_id: str | None = None              # required only if the user belongs to >1 org for this app


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str


class RefreshIn(BaseModel):
    refresh_token: str


class AcceptInviteIn(BaseModel):
    token: str
    password: str = Field(min_length=8)
    full_name: str | None = None


class LogoutIn(BaseModel):
    refresh_token: str


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


class MemberIn(BaseModel):
    email: EmailStr
    password: str | None = None  # required only if the user does not yet exist
    full_name: str | None = None
    org_id: str
    app_key: str
    role_names: list[str] = []


class ProvisionIn(BaseModel):
    """Create a new org + owner for an application in one call (the "sell one app" flow)."""

    email: EmailStr
    password: str | None = None   # required if the user does not yet exist
    full_name: str | None = None
    org_name: str
    app_key: str
    permission_keys: list[str] = []  # defaults to a sensible owner set if empty
