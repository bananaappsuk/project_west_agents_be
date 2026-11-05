"""The step handler contract: what a capability writes, and what it gets back.

A handler is an async callable taking a `StepContext` and returning a `StepResult`.
That is the entire surface a capability has to implement, and it is the only place
capability-specific business logic lives. The engine never imports a handler; it
resolves one by key from a `HandlerRegistry` the host populated at startup.

The result type is a small closed set rather than a bare success/failure boolean,
because the engine has four genuinely different things it can do next and a boolean
would collapse them:

  * `ok`          -- the step is done; move on.
  * `retry`       -- a transient problem; try this step again later, within budget.
  * `fail`        -- a permanent problem; the workflow is finished and did not
                     succeed. Never retried, however much budget is left.
  * `needs_review`-- the step cannot decide and a person must look at it. Not a
                     failure: the run parks in `exception` and stays resumable.

Distinguishing `retry` from `fail` is the handler's job and not the engine's,
because only the handler knows whether its failure was a restart mid-deploy or a
422 from a validation rule -- the same distinction the existing CRM client already
draws when it retries transient errors but never a 422.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .context import ContextBundle
from .definition import StepSpec
from .errors import UnknownHandler
from .principal import Principal


class StepOutcome(str, Enum):
    OK = "ok"
    RETRY = "retry"
    FAIL = "fail"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class StepContext:
    """Everything a handler is given. Deliberately read-only.

    A handler receives the *reference* to the business record (`subject_ref`), not
    a copy of it, and loads what it needs through its own service's data access --
    which is what keeps the CRM and the mail store authoritative and stops the
    workflow store becoming a second master copy.
    """

    run_id: str
    capability: str
    #: The business record this run is about: an email id, a recording id, a case
    #: reference. Opaque to the engine.
    subject_ref: str
    step: StepSpec
    #: 1 on the first execution of this step, 2 on the first retry, and so on.
    #: Handlers that need to be careful about repeating an effect branch on this.
    attempt: int
    #: Who the run is executing for. Authorization has already been checked by the
    #: engine before a handler is called, but a handler performing its own
    #: narrower check (a per-document ACL, say) gets the principal to do it with.
    principal: Principal
    #: Authorised context, assembled per the step's `context_slots`.
    context: ContextBundle
    #: Accumulated outputs of earlier steps in this run, keyed by step name. How a
    #: step hands a value to a later one without the engine knowing what it means.
    outputs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: The payload the run was started with. Non-sensitive parameters only -- it is
    #: persisted on the run.
    payload: Mapping[str, Any] = field(default_factory=dict)
    #: Namespaced per run+step, so a handler's log lines are attributable.
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("platform_orchestration.step")
    )

    def output_of(self, step_name: str) -> Mapping[str, Any]:
        return self.outputs.get(step_name, {})


@dataclass(frozen=True)
class StepResult:
    """What a handler decided. Built through the classmethods, not directly."""

    outcome: StepOutcome
    #: Passed to later steps and persisted on the step record. Audit redaction
    #: applies when it reaches an event, so a handler returning a draft body here
    #: is safe -- the body reaches the next step, never the audit trail.
    output: dict[str, Any] = field(default_factory=dict)
    #: Operator-facing reason. Appears in audit detail, so it must describe the
    #: problem without quoting record content.
    error: str | None = None
    #: Seconds to wait before the next attempt. None lets the engine apply its own
    #: backoff; a handler that knows better (a Retry-After header) can say so.
    retry_after: float | None = None

    @classmethod
    def ok(cls, **output: Any) -> StepResult:
        return cls(StepOutcome.OK, output=dict(output))

    @classmethod
    def retry(cls, error: str, *, retry_after: float | None = None, **output: Any) -> StepResult:
        return cls(StepOutcome.RETRY, output=dict(output), error=error, retry_after=retry_after)

    @classmethod
    def fail(cls, error: str, **output: Any) -> StepResult:
        return cls(StepOutcome.FAIL, output=dict(output), error=error)

    @classmethod
    def needs_review(cls, error: str, **output: Any) -> StepResult:
        return cls(StepOutcome.NEEDS_REVIEW, output=dict(output), error=error)

    @property
    def succeeded(self) -> bool:
        return self.outcome is StepOutcome.OK


#: What a capability actually writes.
StepHandler = Callable[[StepContext], Awaitable[StepResult]]


class HandlerRegistry:
    """Handler key -> implementation, for one host.

    An instance, not a global, for the same reason `DefinitionRegistry` is: the
    library's tests register throwaway handlers without mutating process state.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, StepHandler] = {}

    def register(self, key: str, handler: StepHandler) -> StepHandler:
        if not key:
            raise UnknownHandler("a handler needs a non-empty key")
        if key in self._handlers and self._handlers[key] is not handler:
            # Silently replacing a handler would mean a workflow executing
            # something other than what the service believes it registered, and
            # the symptom would appear in a completely different place.
            raise UnknownHandler(
                "handler %r is already registered; pick a distinct key rather than "
                "overwriting one that definitions may already reference" % key
            )
        self._handlers[key] = handler
        return handler

    def handler(self, key: str) -> Callable[[StepHandler], StepHandler]:
        """Decorator form: `@registry.handler("mail.classify")`."""

        def decorate(fn: StepHandler) -> StepHandler:
            self.register(key, fn)
            return fn

        return decorate

    def get(self, key: str) -> StepHandler:
        handler = self._handlers.get(key)
        if handler is None:
            raise UnknownHandler("no handler registered for %r" % key)
        return handler

    def keys(self) -> frozenset[str]:
        return frozenset(self._handlers)
