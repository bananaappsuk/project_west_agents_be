"""Workflow definitions: what a workflow is, declared as data.

A definition is an ordered list of steps, each naming a handler by *key* rather
than holding a reference to a function. That indirection is the whole point of this
module: the orchestration core resolves a key through a registry the host populated,
so the core imports nothing from Email, Voice, Reports, Scheduling, Invoicing or
Case Management, and adding a capability is a new definition plus new handlers with
no change to the engine.

Two properties on a step are the ones that matter most, and they are deliberately
separate:

  * `requires_approval` -- the engine stops before this step and waits for a human
    decision.
  * `consequential` -- this step does something that reaches outside the system and
    cannot be undone (sends mail, writes to the CRM, files a document).

Keeping them apart is what makes the guarantee checkable. "Nothing consequential
runs before approval" is then a property a definition can be *validated* for
(`WorkflowDefinition.validate`), rather than a convention each capability is trusted
to have remembered. A definition that puts an unapproved send in front of its
approval gate fails to register at startup instead of discovering the problem by
sending something.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import DefinitionError

#: Default retry budget for a step that does not state one. Three attempts is the
#: same shape as the CRM client's existing transient-failure retry: enough to ride
#: out a restart mid-deploy, few enough that a genuinely broken step surfaces fast.
DEFAULT_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class StepSpec:
    """One step of a workflow."""

    #: Unique within its definition. Persisted on the step record and in audit
    #: events, so it is part of the operational interface: renaming a step makes
    #: historical rows refer to a step that no longer exists, which is why
    #: definitions carry a version (see `WorkflowDefinition.version`).
    name: str
    #: The key the host registered its handler under.
    handler: str
    #: Stop and wait for a human before running this step.
    requires_approval: bool = False
    #: This step has effects outside the system that cannot be reversed.
    consequential: bool = False
    #: Whether a *standing policy* may stand in for the human reviewer on this
    #: step. Off by default, so the ordinary reading of `requires_approval` is
    #: unchanged: a person, or nothing.
    #:
    #: It exists because some capabilities are deliberately configured to act on
    #: their own -- the mail agent's auto-reply setting is one, and an
    #: administrator turning it on *is* an authorisation decision. The choice this
    #: models is between recording that decision against each run or leaving the
    #: autonomous path ungated and unaudited, which is what it replaced. It never
    #: widens what a *person* may do, and the engine still refuses a service
    #: principal on any step that has not opted in.
    policy_approval: bool = False
    #: Total attempts, including the first. 1 means "never retry" -- correct for a
    #: step that is not safe to repeat and has no idempotency guard of its own.
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    #: Context slots to retrieve before running the step. Fetched through the
    #: host's ContextProvider, subject to the principal's own entitlements.
    context_slots: tuple[str, ...] = ()
    #: Free-form, passed to the handler. For parameterising a shared handler
    #: across definitions rather than writing near-identical handlers.
    config: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise DefinitionError("a step must have a name")
        if not self.handler:
            raise DefinitionError("step %r must name a handler" % self.name)
        if self.max_attempts < 1:
            raise DefinitionError(
                "step %r: max_attempts must be at least 1, got %d"
                % (self.name, self.max_attempts)
            )
        if self.policy_approval and not self.requires_approval:
            # There is no gate for a policy to satisfy, so the flag would read as
            # a protection that is not there. Catch it at registration rather than
            # let it sit in a definition looking like one.
            raise DefinitionError(
                "step %r sets policy_approval but not requires_approval; a policy "
                "can only stand in for a human at a step that actually waits for one"
                % self.name
            )


@dataclass(frozen=True)
class WorkflowDefinition:
    """An ordered, versioned pipeline of steps for one capability."""

    name: str
    capability: str
    steps: tuple[StepSpec, ...]
    #: Bumped when the step list changes in a way that makes an in-flight run of
    #: the previous shape unresumable. Persisted on the run, so a run always knows
    #: which shape it was started against and a redeployment mid-run cannot
    #: silently re-point it at a different step list.
    version: int = 1
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise DefinitionError("a workflow definition must have a name")
        if not self.capability:
            raise DefinitionError("definition %r must name a capability" % self.name)
        if not self.steps:
            raise DefinitionError("definition %r has no steps" % self.name)
        seen: set[str] = set()
        for step in self.steps:
            if step.name in seen:
                raise DefinitionError(
                    "definition %r has two steps named %r" % (self.name, step.name)
                )
            seen.add(step.name)

    def step(self, index: int) -> StepSpec | None:
        if 0 <= index < len(self.steps):
            return self.steps[index]
        return None

    def index_of(self, step_name: str) -> int:
        for i, step in enumerate(self.steps):
            if step.name == step_name:
                return i
        raise DefinitionError("definition %r has no step %r" % (self.name, step_name))

    @property
    def step_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.steps)

    def validate(self, *, known_handlers: frozenset[str] | None = None) -> None:
        """Check the definition is executable and its approval gates are meaningful.

        Called at registration time, because every problem it catches is one whose
        natural discovery point is otherwise *after* a side effect.
        """
        if known_handlers is not None:
            missing = [s.name for s in self.steps if s.handler not in known_handlers]
            if missing:
                raise DefinitionError(
                    "definition %r names unregistered handlers at step(s): %s"
                    % (self.name, ", ".join(missing))
                )

        # The central guarantee: a consequential step must have an approval gate
        # at or before it. "At" counts -- a step that both requires approval and is
        # consequential is the ordinary shape of "send this reply once a human says
        # so", and the engine stops in front of such a step, not after it.
        approved_from: int | None = None
        for i, step in enumerate(self.steps):
            if step.requires_approval and approved_from is None:
                approved_from = i
            if step.consequential and approved_from is None:
                raise DefinitionError(
                    "definition %r step %r is consequential but no approval gate "
                    "precedes it; a consequential step must be gated, or be "
                    "declared non-consequential deliberately"
                    % (self.name, step.name)
                )


class DefinitionRegistry:
    """The definitions one host knows about.

    An instance rather than a module-level dict: a test builds its own registry
    without reaching into global state, and two hosts in one process (which the
    library's tests do) do not collide. The agent factory's module-level agent
    REGISTRY is the right shape for a process that hosts every agent; a workflow
    registry belongs to the service that executes the workflows.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, WorkflowDefinition] = {}

    def register(
        self,
        definition: WorkflowDefinition,
        *,
        known_handlers: frozenset[str] | None = None,
    ) -> WorkflowDefinition:
        definition.validate(known_handlers=known_handlers)
        existing = self._by_name.get(definition.name)
        if existing is not None and existing != definition:
            raise DefinitionError(
                "definition %r is already registered with a different shape; "
                "bump its version rather than redefining it in place"
                % definition.name
            )
        self._by_name[definition.name] = definition
        return definition

    def get(self, name: str) -> WorkflowDefinition:
        definition = self._by_name.get(name)
        if definition is None:
            raise DefinitionError("no workflow definition named %r" % name)
        return definition

    def has(self, name: str) -> bool:
        return name in self._by_name

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))

    def for_capability(self, capability: str) -> tuple[WorkflowDefinition, ...]:
        return tuple(
            d for _, d in sorted(self._by_name.items()) if d.capability == capability
        )
