"""Password hashing + token minting."""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

from .config import settings
from .keys import private_key

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _ph.verify(password_hash, password)
        return True
    except Argon2Error:
        return False


def create_access_token(*, sub: str, typ: str, org: str | None, aud: str, roles: list[str], scope: str) -> str:
    now = int(time.time())
    payload = {
        "iss": settings.auth_issuer,
        "sub": sub,
        "typ": typ,          # "user" | "agent"
        "org": org,          # tenant
        "aud": aud,          # application key
        "roles": roles,
        "scope": scope,
        "iat": now,
        "exp": now + settings.access_token_ttl,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, private_key(), algorithm="RS256", headers={"kid": settings.key_id})


def new_refresh_token() -> tuple[str, str]:
    """Return (raw_token, sha256_hash). Only the hash is stored."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
