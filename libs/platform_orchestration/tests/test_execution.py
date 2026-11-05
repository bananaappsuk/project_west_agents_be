"""Workflow creation and execution, and the guards around running a step twice."""

from __future__ import annotations

import pytest

from platform_orchestration import StepResult, WorkflowState
from platform_orchestration.repository import utcnow

from .conftest import ALICE

S = WorkflowState


# ── creation ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_new_run_starts_in_created(orchestrator):
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    assert run.state is S.CREATED
    assert run.step_index == 0
    assert run.attempts == 0
    assert run.capability == "test"
    assert run.subject_ref == "email-1"


@pytest.mark.asyncio
async def test_the_run_records_the_definition_version_it_began_on(orchestrator):
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    assert run.version == 1


@pytest.mark.asyncio
async def test_the_payload_reaches_the_handler(orchestrator, handlers):
    seen = {}

    async def capture(ctx):
        seen.update(dict(ctx.payload))
        return StepResult.ok()

    handlers.script["classify"] = lambda ctx: capture(ctx)
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
        payload={"mailbox_id": "mbx-1"},
    )
    await orchestrator.advance(run.id, principal=ALICE)
    assert seen == {"mailbox_id": "mbx-1"}


# ── execution ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_advance_runs_exactly_one_step(orchestrator, handlers):
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    run = await orchestrator.advance(run.id, principal=ALICE)
    assert [name for name, _ in handlers.calls] == ["classify"]
    assert run.state is S.RUNNING
    assert run.step_index == 1


@pytest.mark.asyncio
async def test_run_to_completion_runs_every_step_and_succeeds(orchestrator, handlers):
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    run = await orchestrator.run_to_completion(run.id, principal=ALICE)
    assert [name for name, _ in handlers.calls] == ["classify", "enrich"]
    assert run.state is S.SUCCEEDED
    assert run.finished_at is not None


@pytest.mark.asyncio
async def test_step_output_is_recorded_per_step(orchestrator, handlers, repo):
    handlers.script["classify"] = StepResult.ok(category="new_referral")
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.run_to_completion(run.id, principal=ALICE)
    steps = {s.name: s for s in await repo.steps_for(run.id)}
    assert steps["classify"].output == {"category": "new_referral"}
    assert steps["classify"].state is S.SUCCEEDED


@pytest.mark.asyncio
async def test_a_later_step_sees_an_earlier_step_s_output(orchestrator, handlers):
    handlers.script["classify"] = StepResult.ok(category="new_referral")
    seen = {}

    def capture(ctx):
        seen.update(dict(ctx.output_of("classify")))
        return StepResult.ok()

    handlers.script["enrich"] = capture
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.run_to_completion(run.id, principal=ALICE)
    assert seen == {"category": "new_referral"}


@pytest.mark.asyncio
async def test_advancing_a_finished_run_is_a_no_op(orchestrator, handlers):
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.run_to_completion(run.id, principal=ALICE)
    before = len(handlers.calls)
    run = await orchestrator.advance(run.id, principal=ALICE)
    assert run.state is S.SUCCEEDED
    assert len(handlers.calls) == before


# ── duplicate prevention: creation ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_same_idempotency_key_returns_the_same_run(orchestrator):
    """A cron tick re-seeing the same email must not start a second workflow."""
    first = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
        idempotency_key="email-1",
    )
    second = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
        idempotency_key="email-1",
    )
    assert second.id == first.id


@pytest.mark.asyncio
async def test_a_suppressed_duplicate_is_audited(orchestrator, audit):
    await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
        idempotency_key="email-1",
    )
    await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
        idempotency_key="email-1",
    )
    assert audit.actions().count("duplicate_suppressed") == 1


@pytest.mark.asyncio
async def test_no_key_means_a_new_run_each_time(orchestrator):
    first = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    second = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    assert first.id != second.id


@pytest.mark.asyncio
async def test_the_same_key_on_a_different_definition_is_a_different_run(orchestrator):
    """The key is scoped per definition: triaging and reporting on the same email
    are two workflows, not a duplicate of one."""
    first = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
        idempotency_key="email-1",
    )
    second = await orchestrator.start(
        definition="test.gated", subject_ref="email-1", principal=ALICE,
        idempotency_key="email-1",
    )
    assert first.id != second.id


# ── duplicate prevention: execution ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_succeeded_step_is_never_re_run(orchestrator, handlers, repo):
    """The resume guard. A run interrupted after its side effect but before its
    state was persisted must not repeat the effect."""
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.advance(run.id, principal=ALICE)
    assert [n for n, _ in handlers.calls] == ["classify"]

    # Rewind the run's cursor, as a crash between the step record commit and the
    # run commit would leave it, and advance again.
    stored = await repo.get_run(run.id)
    stored.step_index = 0
    await repo.save_run(stored)

    await orchestrator.advance(run.id, principal=ALICE)
    assert [n for n, _ in handlers.calls] == ["classify"], "classify ran twice"


@pytest.mark.asyncio
async def test_skipping_a_done_step_is_audited(orchestrator, audit, repo):
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.advance(run.id, principal=ALICE)
    stored = await repo.get_run(run.id)
    stored.step_index = 0
    await repo.save_run(stored)
    await orchestrator.advance(run.id, principal=ALICE)
    assert "step_already_done" in audit.actions()


@pytest.mark.asyncio
async def test_a_leased_run_is_not_claimed_by_a_second_worker(orchestrator, repo, handlers):
    """Two workers racing produce one winner, so one outbound effect."""
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    # Simulate a live lease held by another worker mid-step.
    stored = await repo.get_run(run.id)
    stored.state = S.RUNNING
    stored.lease_expires_at = utcnow().replace(year=utcnow().year + 1)
    await repo.save_run(stored)

    returned = await orchestrator.advance(run.id, principal=ALICE)
    assert handlers.calls == [], "the second worker executed a leased run"
    assert returned.state is S.RUNNING


# ── definition version safety ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_run_started_on_an_older_definition_parks_for_review(
    orchestrator, repo, handlers
):
    """A redeploy that changes a definition must not re-point an in-flight run."""
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    stored = await repo.get_run(run.id)
    stored.version = 99          # as if the deployed definition had moved on
    await repo.save_run(stored)

    run = await orchestrator.advance(run.id, principal=ALICE)
    assert run.state is S.EXCEPTION
    assert handlers.calls == []
    assert "version" in (run.last_error or "")
