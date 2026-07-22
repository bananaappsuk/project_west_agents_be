"""RSA signing key management + JWKS.

Priority: PRIVATE_KEY_PEM env (cloud) -> ./keys/private.pem (dev) -> generate a new
key and persist it to ./keys/private.pem. The public half is published at
`/.well-known/jwks.json` so every service can verify tokens offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .config import settings

_KEY_PATH = Path("keys/private.pem")


def _load_or_create_pem() -> str:
    if settings.private_key_pem:
        return settings.private_key_pem
    if _KEY_PATH.exists():
        return _KEY_PATH.read_text()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _KEY_PATH.write_text(pem)
    return pem


_private_key = serialization.load_pem_private_key(_load_or_create_pem().encode(), password=None)
_public_key = _private_key.public_key()


def private_key():
    return _private_key


def public_key():
    return _public_key


def jwks() -> dict:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_public_key))
    jwk.update({"kid": settings.key_id, "use": "sig", "alg": "RS256"})
    return {"keys": [jwk]}
