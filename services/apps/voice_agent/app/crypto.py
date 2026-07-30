"""Symmetric encryption for the stored BT Cloud client secret (Fernet)."""

from __future__ import annotations

from cryptography.fernet import Fernet

from .config import settings

if not settings.encryption_key:
    raise RuntimeError(
        "ENCRYPTION_KEY is required. Generate one with:\n"
        '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
    )

_fernet = Fernet(settings.encryption_key.encode())


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _fernet.decrypt(ciphertext.encode()).decode()
