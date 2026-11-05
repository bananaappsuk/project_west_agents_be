"""Definition validation, the handler registry, and authorised context retrieval.

The theme is that a mistake in a capability's wiring should fail at startup, not
part-way through a run that has already had effects. The context tests cover the
other half of the brief's requirement: a workflow sees only what the principal it
acts for is entitled to, and whatever it does see is traceable to where it came
from.
"""

from __future__ import annotations

import pytest

from platform_orchestration import (
    AllowAll,
    ContextBundle,
    ContextFragment,
    DefinitionError,
    DefinitionRegistry,
    HandlerRegistry,
    NullContextProvider,
    Orchestrator,
    SourceReference,
    StepResult,
    StepSpec,
    UnknownHandler,
    WorkflowDefinition,
    WorkflowState,
)

from .conftest import ALICE

S = WorkflowState


# ── definition validation ────────────────────────────────────────────────────

def test_a_definition_needs_a_name():
    with pytest.raises(DefinitionError):
        WorkflowDefinition(name="", capability="mail", steps=(
            StepSpec(name="a", handler="h"),
        ))


def test_a_definition_needs_a_capability():
    with pytest.raises(DefinitionError):
        WorkflowDefinition(name="w", capability="", steps=(
            StepSpec(name="a", handler="h"),
        ))


def test_a_definition_needs_at_least_one_step():
    with pytest.raises(DefinitionError):
        WorkflowDefinition(name="w", capability="mail", steps=())


def test_duplicate_step_names_are_rejected():
    """Step names key the step records, so two of a name would collide and the
    second would read the first's result as its own."""
    with pytest.raises(DefinitionError) as exc:
        WorkflowDefinition(name="w", capability="mail", steps=(
            StepSpec(name="a", handler="h"),
            StepSpec(name="a", handler="h2"),
        ))
    assert "two steps named" in str(exc.value)


def test_a_step_needs_a_handler():
    with pytest.raises(DefinitionError):
        StepSpec(name="a", handler="")


def test_max_attempts_below_one_is_rejected():
    with pytest.raises(DefinitionError):
        StepSpec(name="a", handler="h", max_attempts=0)


def test_max_attempts_of_one_is_allowed_for_a_step_unsafe_to_repeat():
    assert StepSpec(name="a", handler="h", max_attempts=1).max_attempts == 1


def test_registering_against_unknown_handlers_fails_at_startup():
    """Discovering an unroutable third step after performing the first two means
    effects that cannot be taken back."""
    registry = DefinitionRegistry()
    with pytest.raises(DefinitionError) as exc:
        registry.register(
            WorkflowDefinition(name="w", capability="mail", steps=(
                StepSpec(name="a", handler="nope"),
            )),
            known_handlers=frozenset({"yes"}),
        )
    assert "unregistered handlers" in str(exc.value)


def test_redefining_a_definition_differently_is_refused():
    registry = DefinitionRegistry()
    first = WorkflowDefinition(name="w", capability="mail", steps=(
        StepSpec(name="a", handler="h"),
    ))
    second = WorkflowDefinition(name="w", capability="mail", steps=(
        StepSpec(name="b", handler="h"),
    ))
    registry.register(first)
    with pytest.raises(DefinitionError) as exc:
        registry.register(second)
    assert "bump its version" in str(exc.value)


def test_registering_the_identical_definition_twice_is_fine():
    """Two import paths reaching the same registration must not crash startup."""
    registry = DefinitionRegistry()
    definition = WorkflowDefinition(name="w", capability="mail", steps=(
        StepSpec(name="a", handler="h"),
    ))
    registry.register(definition)
    registry.register(definition)
    assert registry.names() == ("w",)


def test_an_unknown_definition_name_is_an_error():
    with pytest.raises(DefinitionError):
        DefinitionRegistry().get("nope")


def test_definitions_can_be_listed_per_capability():
    registry = DefinitionRegistry()
    registry.register(WorkflowDefinition(name="m", capability="mail", steps=(
        StepSpec(name="a", handler="h"),
    )))
    registry.register(WorkflowDefinition(name="v", capability="voice", steps=(
        StepSpec(name="a", handler="h"),
    )))
    assert [d.name for d in registry.for_capability("voice")] == ["v"]


def test_index_of_finds_a_step_by_name():
    definition = WorkflowDefinition(name="w", capability="mail", steps=(
        StepSpec(name="a", handler="h"), StepSpec(name="b", handler="h"),
    ))
    assert definition.index_of("b") == 1
    with pytest.raises(DefinitionError):
        definition.index_of("c")


# ── handler registry ─────────────────────────────────────────────────────────

def test_a_handler_can_be_registered_by_decorator():
    registry = HandlerRegistry()

    @registry.handler("mail.classify")
    async def classify(ctx):
        return StepResult.ok()

    assert registry.get("mail.classify") is classify


def test_an_unregistered_handler_key_raises():
    with pytest.raises(UnknownHandler):
        HandlerRegistry().get("nope")


def test_a_handler_key_cannot_be_silently_overwritten():
    """A workflow executing something other than what the service registered
    would show its symptom somewhere else entirely."""
    registry = HandlerRegistry()

    async def first(ctx):
        return StepResult.ok()

    async def second(ctx):
        return StepResult.ok()

    registry.register("k", first)
    with pytest.raises(UnknownHandler):
        registry.register("k", second)


def test_re_registering_the_same_function_is_fine():
    registry = HandlerRegistry()

    async def handler(ctx):
        return StepResult.ok()

    registry.register("k", handler)
    registry.register("k", handler)
    assert registry.keys() == frozenset({"k"})


def test_an_empty_handler_key_is_refused():
    with pytest.raises(UnknownHandler):
        HandlerRegistry().register("", lambda ctx: None)


# ── step results ─────────────────────────────────────────────────────────────

def test_the_result_constructors_set_the_right_outcome():
    assert StepResult.ok(a=1).succeeded
    assert not StepResult.retry("e").succeeded
    assert not StepResult.fail("e").succeeded
    assert not StepResult.needs_review("e").succeeded


def test_a_result_carries_its_output():
    assert StepResult.ok(category="x").output == {"category": "x"}


# ── context: authorisation and provenance ────────────────────────────────────

class ScopedContextProvider:
    """Serves the case only to principals on its allow-list.

    Stands in for the real thing: a provider reaching the CRM or SharePoint on
    behalf of a principal, which returns nothing where that principal is not
    entitled -- rather than returning the record and leaving the caller to filter.
    """

    def __init__(self, allowed_users):
        self.allowed = set(allowed_users)
        self.asked_for = []

    async def fetch(self, request):
        self.asked_for.append((request.principal.user_id, request.slots))
        if request.principal.user_id not in self.allowed:
            return ContextBundle(withheld=tuple(request.slots))
        return ContextBundle(fragments=(
            ContextFragment(
                source=SourceReference(
                    system="crm", ref="PW-C-2026-0007", label="Case 0007",
                ),
                data={"case_ref": "PW-C-2026-0007", "notes": "sensitive"},
            ),
        ))


@pytest.fixture
def ctx_definitions(handlers):
    registry = DefinitionRegistry()
    registry.register(
        WorkflowDefinition(
            name="test.with_context",
            capability="test",
            steps=(
                StepSpec(
                    name="classify", handler="test.classify",
                    context_slots=("case",),
                ),
            ),
        ),
        known_handlers=handlers.keys(),
    )
    return registry


@pytest.mark.asyncio
async def test_a_step_receives_context_it_is_entitled_to(
    repo, ctx_definitions, handlers, audit
):
    provider = ScopedContextProvider(allowed_users={"alice"})
    orch = Orchestrator(
        repository=repo, definitions=ctx_definitions, handlers=handlers,
        authorization=AllowAll(), audit=audit, context_provider=provider,
    )
    seen = {}

    def capture(ctx):
        seen["refs"] = [r.key() for r in ctx.context.references()]
        seen["withheld"] = ctx.context.withheld
        return StepResult.ok()

    handlers.script["classify"] = capture
    run = await orch.start(
        definition="test.with_context", subject_ref="email-1", principal=ALICE,
    )
    await orch.advance(run.id, principal=ALICE)
    assert seen["refs"] == ["crm:PW-C-2026-0007"]
    assert seen["withheld"] == ()


@pytest.mark.asyncio
async def test_context_is_withheld_from_a_principal_who_is_not_entitled(
    repo, ctx_definitions, handlers, audit
):
    provider = ScopedContextProvider(allowed_users=set())
    orch = Orchestrator(
        repository=repo, definitions=ctx_definitions, handlers=handlers,
        authorization=AllowAll(), audit=audit, context_provider=provider,
    )
    seen = {}

    def capture(ctx):
        seen["refs"] = [r.key() for r in ctx.context.references()]
        seen["withheld"] = ctx.context.withheld
        return StepResult.ok()

    handlers.script["classify"] = capture
    run = await orch.start(
        definition="test.with_context", subject_ref="email-1", principal=ALICE,
    )
    await orch.advance(run.id, principal=ALICE)
    assert seen["refs"] == []
    # Explicitly withheld, so a handler can tell "not authorised" from "empty" --
    # otherwise it would treat a withheld case as a case with no history.
    assert seen["withheld"] == ("case",)


@pytest.mark.asyncio
async def test_the_provider_is_asked_on_behalf_of_the_principal(
    repo, ctx_definitions, handlers, audit
):
    provider = ScopedContextProvider(allowed_users={"alice"})
    orch = Orchestrator(
        repository=repo, definitions=ctx_definitions, handlers=handlers,
        authorization=AllowAll(), audit=audit, context_provider=provider,
    )
    run = await orch.start(
        definition="test.with_context", subject_ref="email-1", principal=ALICE,
    )
    await orch.advance(run.id, principal=ALICE)
    assert provider.asked_for == [("alice", ("case",))]


@pytest.mark.asyncio
async def test_provenance_is_persisted_on_the_run(
    repo, ctx_definitions, handlers, audit
):
    """A summary that cannot be traced back to the case it was drawn from is a
    summary nobody can check."""
    provider = ScopedContextProvider(allowed_users={"alice"})
    orch = Orchestrator(
        repository=repo, definitions=ctx_definitions, handlers=handlers,
        authorization=AllowAll(), audit=audit, context_provider=provider,
    )
    run = await orch.start(
        definition="test.with_context", subject_ref="email-1", principal=ALICE,
    )
    await orch.advance(run.id, principal=ALICE)
    stored = await repo.get_run(run.id)
    assert stored.metadata["sources"] == ["crm:PW-C-2026-0007"]


@pytest.mark.asyncio
async def test_withheld_slots_are_recorded_on_the_run(
    repo, ctx_definitions, handlers, audit
):
    provider = ScopedContextProvider(allowed_users=set())
    orch = Orchestrator(
        repository=repo, definitions=ctx_definitions, handlers=handlers,
        authorization=AllowAll(), audit=audit, context_provider=provider,
    )
    run = await orch.start(
        definition="test.with_context", subject_ref="email-1", principal=ALICE,
    )
    await orch.advance(run.id, principal=ALICE)
    stored = await repo.get_run(run.id)
    assert stored.metadata["context_withheld"] == ["case"]


@pytest.mark.asyncio
async def test_case_content_does_not_reach_the_audit_trail(
    repo, ctx_definitions, handlers, audit
):
    """Only the reference is audited. The content stays where its own access
    controls apply."""
    provider = ScopedContextProvider(allowed_users={"alice"})
    orch = Orchestrator(
        repository=repo, definitions=ctx_definitions, handlers=handlers,
        authorization=AllowAll(), audit=audit, context_provider=provider,
    )
    run = await orch.start(
        definition="test.with_context", subject_ref="email-1", principal=ALICE,
    )
    await orch.advance(run.id, principal=ALICE)
    started = audit.for_action("step_started")[0]
    assert started.sources == ("crm:PW-C-2026-0007",)
    assert "sensitive" not in str([e.detail for e in audit.events])


@pytest.mark.asyncio
async def test_no_provider_is_asked_when_a_step_declares_no_slots(
    orchestrator, handlers
):
    """The default provider is never consulted for a step that needs nothing."""
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    run = await orchestrator.advance(run.id, principal=ALICE)
    assert run.state is S.RUNNING


@pytest.mark.asyncio
async def test_the_null_provider_reports_every_slot_as_withheld():
    from platform_orchestration import ContextRequest
    bundle = await NullContextProvider().fetch(ContextRequest(
        principal=ALICE, capability="test", subject_ref="e1", slots=("case", "docs"),
    ))
    assert bundle.fragments == ()
    assert bundle.withheld == ("case", "docs")


def test_a_bundle_can_be_queried_by_source_system():
    bundle = ContextBundle(fragments=(
        ContextFragment(source=SourceReference(system="crm", ref="c1")),
        ContextFragment(source=SourceReference(system="sharepoint", ref="d1")),
    ))
    assert [f.source.ref for f in bundle.get("crm")] == ["c1"]
    assert bundle.was_withheld("case") is False
