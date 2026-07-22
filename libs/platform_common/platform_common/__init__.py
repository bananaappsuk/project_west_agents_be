"""Shared platform library used by every service.

Exposes:
- BaseServiceSettings  — common env-driven config
- Auth                 — JWT verification + scope-enforcement FastAPI dependencies
- make_db              — async SQLAlchemy engine/session for Neon Postgres
"""

from .config import BaseServiceSettings
from .security import Auth

__all__ = ["BaseServiceSettings", "Auth"]
