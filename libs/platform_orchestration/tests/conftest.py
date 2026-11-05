"""Shared fixtures: a working orchestrator wired entirely to in-memory ports.

No database and no network, matching how the services in this repo test. The
repository under test is the real `InMemoryWorkflowRepository`, not a mock, so the
engine's compare-and-set, retry sweep and step-skipping behaviour are genuinely
exercised rather than stubbed into always agreeing.
"""

from __future__ import annotations

import inspect

import pytest

from platform_orchestration import (
    AllowAll,
    DefinitionRegistry,
    HandlerRegistry,
    InMemoryAuditSink,
    InMemoryWorkflowRepository,
    Orchestrator,
    Principal,
    StepResult,
    StepSpec,
    WorkflowDefinition,
)

ALICE = Principal(user_id="alice", groups=frozenset({"g-coordinator"}))
BOB = Principal(user_id="bob", groups=frozenset({"g-readonly"}))
CRON = Principal(user_id="", service=True)


@pytest.fixture
def repo():
    return InMemoryWorkflowRepository()


@pytest.fixture
def audit():
    return InMemoryAuditSink()


@pytest.fixture
def handlers():
    """Handlers whose behaviour a test can steer through `calls` and `script`.

    `calls` records every invocation so a test can assert a step ran once and not
    twice -- the duplicate-prevention assertions depend on counting, not on
    inspecting state after the fact.
    """
    registry = HandlerRegistry()
    registry.calls = []          # type: ignore[attr-defined]
    registry.script = {}         # type: ignore[attr-defined]

    def _record(name):
        async def handler(ctx):
            registry.calls.append((name, ctx.attempt))   # type: ignore[attr-defined]
            scripted = registry.script.get(name)         # type: ignore[attr-defined]
            if scripted is None:
                return StepResult.ok(ran=name)
            result = scripted(ctx) if callable(scripted) else scripted
            # A scripted handler may be a plain function or a coroutine function;
            # awaiting only what is awaitable lets a test write either.
            if inspect.isawaitable(result):
                result = await result
            return result
        return handler

    for key in ("classify", "enrich", "send", "file"):
        registry.register("test.%s" % key, _record(key))
    return registry


@pytest.fixture
def definitions(handlers):
    registry = DefinitionRegistry()
    registry.register(
        WorkflowDefinition(
            name="test.simple",
            capability="test",
            steps=(
                StepSpec(name="classify", handler="test.classify"),
                StepSpec(name="enrich", handler="test.enrich"),
            ),
        ),
        known_handlers=handlers.keys(),
    )
    registry.register(
        WorkflowDefinition(
            name="test.gated",
            capability="test",
            steps=(
                StepSpec(name="classify", handler="test.classify"),
                StepSpec(
                    name="send", handler="test.send",
                    requires_approval=True, consequential=True,
                ),
                StepSpec(name="file", handler="test.file"),
            ),
        ),
        known_handlers=handlers.keys(),
    )
    registry.register(
        WorkflowDefinition(
            name="test.policy_gated",
            capability="test",
            steps=(
                StepSpec(name="classify", handler="test.classify"),
                StepSpec(
                    name="send", handler="test.send",
                    requires_approval=True, consequential=True,
                    # Opted in: a standing policy may release this one. The same
                    # gate, a different authority -- see test_policy_approval.py.
                    policy_approval=True,
                ),
            ),
        ),
        known_handlers=handlers.keys(),
    )
    return registry


@pytest.fixture
def orchestrator(repo, definitions, handlers, audit):
    return Orchestrator(
        repository=repo,
        definitions=definitions,
        handlers=handlers,
        authorization=AllowAll(),
        audit=audit,
        backoff_base=0.0,      # tests assert on scheduling, never wait on it
        backoff_cap=0.0,
    )
