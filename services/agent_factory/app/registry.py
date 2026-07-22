"""Agent registry — one module holds every application's agents.

Agents register themselves with @register; the runtime resolves them by id
("<app>.<key>"). Adding an agent is a new file under app/agents/ imported in
app/agents/__init__.py — no change to the framework.
"""

from __future__ import annotations

from .base import BaseAgent

REGISTRY: dict[str, type[BaseAgent]] = {}


def register(cls: type[BaseAgent]) -> type[BaseAgent]:
    REGISTRY[f"{cls.app}.{cls.key}"] = cls
    return cls
