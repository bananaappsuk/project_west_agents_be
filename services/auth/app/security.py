"""Token minting."""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid

import jwt

from .config import settings
from .keys import private_key


def create_access_token(
    *, sub: str, typ: str, org: str | None, aud: str, roles: list[str], scope: str,
    groups: list[str] | None = None,
) -> str:
    """Mint an access token.

    `groups` carries the signer's Entra security-group object ids, when the sign-in
    came from Entra ID. The mail agent resolves those against its own
    mailbox_access_rule table to decide which shared mailboxes the person may read
    and send from -- see services/apps/mail_agent/app/access_resolver.py.

    Only the raw group ids travel in the token, never the resolved mailbox set. The
    resolved set lives in the database so that removing someone from a group takes
    effect at their next request rather than whenever their access token happens to
    expire; for safeguarding material that lag is not acceptable.

    Omitted entirely when empty rather than sent as [], so a consumer can tell "this
    token carries no group information" apart from "this user is in no groups" --
    the first must fail closed and be investigated, the second is simply a user with
    no access yet.
    """
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
    if groups:
        payload["groups"] = groups
    return jwt.encode(payload, private_key(), algorithm="RS256", headers={"kid": settings.key_id})


def new_refresh_token() -> tuple[str, str]:
    """Return (raw_token, sha256_hash). Only the hash is stored."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
