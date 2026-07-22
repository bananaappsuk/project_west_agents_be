"""The contract every agent implements.

An agent belongs to one application, has a key, and produces a LangGraph runnable
(a compiled StateGraph with a checkpointer). Knowledge is isolated per agent — each
agent binds to its own vector collection. The framework here is shared; the agent's
prompt, tools, knowledge, and wiring are its own.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseAgent(ABC):
    app: str   # e.g. "west-agent"
    key: str   # e.g. "voice"

    @property
    def id(self) -> str:
        return f"{self.app}.{self.key}"

    @abstractmethod
    def build(self) -> Any:
        """Return a compiled LangGraph runnable (with a checkpointer for multi-turn)."""
        raise NotImplementedError
