"""Human approval: the gate, and the guarantee that nothing consequential
precedes it.

The central assertion in this file is `test_a_consequential_step_does_not_run_
before_approval`: the handler for a gated step must not be called at all until a
person has decided. Everything else here protects that property from the edges --
a service account approving its own work, a rejection being reconsidered, an
approval arriving for a run that is not waiting.
"""

from __future__ import annotations

import pytest

from platform_orchestration import (
    DefinitionError,
    InvalidTransition,
    NotAuthorized,
    StepSpec,
    WorkflowDefinition,
    WorkflowState,
)

from .conftest import ALICE, CRON

S = WorkflowState


async def _to_the_gate(orchestrator, principal=ALICE):
    run = await orchestrator.start(
        definition="test.gated", subject_ref="email-1", principal=principal,
    )
    return await orchestrator.run_to_completion(run.id, principal=principal)


# ── the gate holds ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execution_stops_at_the_gate(orchestrator):
    run = await _to_the_gate(orchestrator)
    assert run.state is S.AWAITING_APPROVAL
    assert run.step_index == 1          # parked ON the gated step, not past it


@pytest.mark.asyncio
async def test_a_consequential_step_does_not_run_before_approval(orchestrator, handlers):
    """The guarantee the whole approval mechanism exists for."""
    await _to_the_gate(orchestrator)
    assert [n for n, _ in handlers.calls] == ["classify"]
    assert "send" not in [n for n, _ in handlers.calls]


@pytest.mark.asyncio
async def test_advancing_a_waiting_run_does_nothing(orchestrator, handlers):
    """A scheduler sweep must not be able to push a run through its own gate."""
    run = await _to_the_gate(orchestrator)
    before = list(handlers.calls)
    run = await orchestrator.advance(run.id, principal=ALICE)
    assert run.state is S.AWAITING_APPROVAL
    assert handlers.calls == before


@pytest.mark.asyncio
async def test_run_to_completion_cannot_walk_through_the_gate(orchestrator, handlers):
    run = await _to_the_gate(orchestrator)
    run = await orchestrator.run_to_completion(run.id, principal=ALICE)
    assert run.state is S.AWAITING_APPROVAL
    assert "send" not in [n for n, _ in handlers.calls]


# ── approving ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_approval_moves_the_run_to_approved(orchestrator):
    run = await _to_the_gate(orchestrator)
    run = await orchestrator.submit_approval(run.id, principal=ALICE, approved=True)
    assert run.state is S.APPROVED


@pytest.mark.asyncio
async def test_the_decision_is_persisted_with_its_author(orchestrator, repo):
    run = await _to_the_gate(orchestrator)
    await orchestrator.submit_approval(
        run.id, principal=ALICE, approved=True, note="checked with the coordinator",
    )
    approval = await repo.approval_for(run.id, "send")
    assert approval is not None
    assert approval.decision == "approved"
    assert approval.decided_by == "alice"
    assert approval.note == "checked with the coordinator"


@pytest.mark.asyncio
async def test_the_gated_step_runs_once_approved(orchestrator, handlers):
    run = await _to_the_gate(orchestrator)
    run = await orchestrator.approve_and_continue(run.id, principal=ALICE, approved=True)
    assert [n for n, _ in handlers.calls] == ["classify", "send", "file"]
    assert run.state is S.SUCCEEDED


@pytest.mark.asyncio
async def test_approval_is_recorded_before_the_step_runs(orchestrator, repo, handlers):
    """A send that happens with no durable record of who authorised it is the
    failure this ordering prevents."""
    order = []

    async def send(ctx):
        approval = await repo.approval_for(ctx.run_id, "send")
        order.append(("send_ran", approval is not None))
        from platform_orchestration import StepResult
        return StepResult.ok()

    handlers.script["send"] = send
    run = await _to_the_gate(orchestrator)
    await orchestrator.approve_and_continue(run.id, principal=ALICE, approved=True)
    assert order == [("send_ran", True)]


# ── rejecting ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rejection_stops_the_workflow(orchestrator, handlers):
    run = await _to_the_gate(orchestrator)
    run = await orchestrator.submit_approval(run.id, principal=ALICE, approved=False)
    assert run.state is S.REJECTED
    assert "send" not in [n for n, _ in handlers.calls]


@pytest.mark.asyncio
async def test_a_rejection_is_persisted_rather_than_implied_by_absence(orchestrator, repo):
    run = await _to_the_gate(orchestrator)
    await orchestrator.submit_approval(
        run.id, principal=ALICE, approved=False, note="not appropriate to reply",
    )
    approval = await repo.approval_for(run.id, "send")
    assert approval.decision == "rejected"
    assert approval.decided_by == "alice"


@pytest.mark.asyncio
async def test_a_rejected_run_cannot_be_approved_afterwards(orchestrator):
    run = await _to_the_gate(orchestrator)
    await orchestrator.submit_approval(run.id, principal=ALICE, approved=False)
    with pytest.raises(InvalidTransition):
        await orchestrator.submit_approval(run.id, principal=ALICE, approved=True)


@pytest.mark.asyncio
async def test_a_rejected_run_does_not_resume_on_a_sweep(orchestrator, handlers):
    run = await _to_the_gate(orchestrator)
    await orchestrator.submit_approval(run.id, principal=ALICE, approved=False)
    before = list(handlers.calls)
    run = await orchestrator.advance(run.id, principal=ALICE)
    assert run.state is S.REJECTED
    assert handlers.calls == before


@pytest.mark.asyncio
async def test_approve_and_continue_on_a_rejection_does_not_execute(orchestrator, handlers):
    run = await _to_the_gate(orchestrator)
    run = await orchestrator.approve_and_continue(run.id, principal=ALICE, approved=False)
    assert run.state is S.REJECTED
    assert "send" not in [n for n, _ in handlers.calls]


# ── who may approve ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_service_principal_cannot_approve(orchestrator):
    """"The system approved it" is not human review. A cron account holding the
    internal key must not be able to clear its own gate."""
    run = await _to_the_gate(orchestrator)
    with pytest.raises(NotAuthorized):
        await orchestrator.submit_approval(run.id, principal=CRON, approved=True)


@pytest.mark.asyncio
async def test_a_refused_service_approval_leaves_the_run_waiting(orchestrator, repo):
    run = await _to_the_gate(orchestrator)
    with pytest.raises(NotAuthorized):
        await orchestrator.submit_approval(run.id, principal=CRON, approved=True)
    stored = await repo.get_run(run.id)
    assert stored.state is S.AWAITING_APPROVAL
    assert await repo.approval_for(run.id, "send") is None


# ── approving at the wrong moment ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_run_that_is_not_waiting_cannot_be_approved(orchestrator):
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    with pytest.raises(InvalidTransition):
        await orchestrator.submit_approval(run.id, principal=ALICE, approved=True)


@pytest.mark.asyncio
async def test_a_finished_run_cannot_be_approved(orchestrator):
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.run_to_completion(run.id, principal=ALICE)
    with pytest.raises(InvalidTransition):
        await orchestrator.submit_approval(run.id, principal=ALICE, approved=True)


# ── definitions are validated for gate coverage ──────────────────────────────

def test_a_consequential_step_with_no_gate_is_rejected_at_registration(definitions, handlers):
    """Caught at startup, because the natural discovery point otherwise is after
    something has been sent."""
    with pytest.raises(DefinitionError) as exc:
        definitions.register(
            WorkflowDefinition(
                name="test.ungated",
                capability="test",
                steps=(
                    StepSpec(name="classify", handler="test.classify"),
                    StepSpec(name="send", handler="test.send", consequential=True),
                ),
            ),
            known_handlers=handlers.keys(),
        )
    assert "consequential" in str(exc.value)


def test_a_gate_on_the_consequential_step_itself_is_enough(definitions, handlers):
    """The ordinary shape: "send this, once a human says so"."""
    definitions.register(
        WorkflowDefinition(
            name="test.gate_on_step",
            capability="test",
            steps=(
                StepSpec(
                    name="send", handler="test.send",
                    requires_approval=True, consequential=True,
                ),
            ),
        ),
        known_handlers=handlers.keys(),
    )
    assert definitions.has("test.gate_on_step")


def test_an_earlier_gate_covers_a_later_consequential_step(definitions, handlers):
    definitions.register(
        WorkflowDefinition(
            name="test.earlier_gate",
            capability="test",
            steps=(
                StepSpec(name="classify", handler="test.classify", requires_approval=True),
                StepSpec(name="send", handler="test.send", consequential=True),
                StepSpec(name="file", handler="test.file", consequential=True),
            ),
        ),
        known_handlers=handlers.keys(),
    )
    assert definitions.has("test.earlier_gate")
