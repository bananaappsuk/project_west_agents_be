"""Resolve and cache compiled agents by id."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from .registry import REGISTRY


@lru_cache
def get_agent(agent_id: str) -> Any | None:
    """Return the compiled runnable for an agent id, or None if unregistered.

    Cached so each agent's graph is built once per process.
    """
    cls = REGISTRY.get(agent_id)
    if cls is None:
        return None
    return cls().build()
