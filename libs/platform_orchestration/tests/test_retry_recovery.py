"""Retry, failure handling and recovery from an interrupted run.

The distinction these tests pin down is between *transient* and *permanent*
failure. A transient failure is retried within a budget; a permanent one is not
retried at all, however much budget remains, because repeating a request the other
side rejected on its merits only wastes time and clutters the audit trail. The
same distinction the existing CRM client already draws when it retries a dropped
connection but never a 422.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from platform_orchestration import (
    AllowAll,
    Orchestrator,
    StepResult,
    StepSpec,
    WorkflowDefinition,
    WorkflowState,
)
from platform_orchestration.repository import utcnow

from .conftest import ALICE

S = WorkflowState


@pytest.fixture
def budgeted(definitions, handlers):
    """A definition whose first step may be attempted twice in total."""
    definitions.register(
        WorkflowDefinition(
            name="test.budgeted",
            capability="test",
            steps=(
                StepSpec(name="classify", handler="test.classify", max_attempts=2),
                StepSpec(name="enrich", handler="test.enrich"),
            ),
        ),
        known_handlers=handlers.keys(),
    )
    return definitions


# ── transient failure is retried ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_retry_result_schedules_another_attempt(orchestrator, handlers):
    handlers.script["classify"] = StepResult.retry("CRM restarting")
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    run = await orchestrator.advance(run.id, principal=ALICE)
    assert run.state is S.RETRY_SCHEDULED
    assert run.attempts == 1
    assert run.next_attempt_at is not None
    assert run.step_index == 0, "a retry must not advance past the failed step"


@pytest.mark.asyncio
async def test_an_unhandled_exception_is_treated_as_transient(orchestrator, handlers):
    """A dropped connection or a restart mid-deploy escapes as an exception far
    more often than it arrives as a considered permanent failure."""
    def boom(ctx):
        raise RuntimeError("connection reset")

    handlers.script["classify"] = boom
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    run = await orchestrator.advance(run.id, principal=ALICE)
    assert run.state is S.RETRY_SCHEDULED


@pytest.mark.asyncio
async def test_a_retry_eventually_succeeds_and_the_run_continues(orchestrator, handlers):
    attempts = {"n": 0}

    def flaky(ctx):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return StepResult.retry("transient")
        return StepResult.ok()

    handlers.script["classify"] = flaky
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    run = await orchestrator.run_to_completion(run.id, principal=ALICE)
    assert run.state is S.SUCCEEDED
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_the_attempt_counter_resets_on_the_next_step(orchestrator, handlers):
    attempts = {"n": 0}

    def flaky(ctx):
        attempts["n"] += 1
        return StepResult.retry("transient") if attempts["n"] == 1 else StepResult.ok()

    handlers.script["classify"] = flaky
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    run = await orchestrator.run_to_completion(run.id, principal=ALICE)
    # Both steps are done and the counter is clear, so the next step gets its own
    # full budget rather than inheriting what the previous one spent.
    assert run.state is S.SUCCEEDED
    assert run.attempts == 0


@pytest.mark.asyncio
async def test_a_handler_may_set_its_own_retry_delay(orchestrator, handlers):
    handlers.script["classify"] = StepResult.retry("rate limited", retry_after=120)
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    run = await orchestrator.advance(run.id, principal=ALICE)
    delay = (run.next_attempt_at - utcnow()).total_seconds()
    assert 100 < delay <= 120


# ── the budget runs out ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_run_fails_once_the_budget_is_exhausted(
    budgeted, handlers, repo, audit
):
    orch = Orchestrator(
        repository=repo, definitions=budgeted, handlers=handlers,
        authorization=AllowAll(), audit=audit, backoff_base=0.0, backoff_cap=0.0,
    )
    handlers.script["classify"] = StepResult.retry("always down")
    run = await orch.start(
        definition="test.budgeted", subject_ref="email-1", principal=ALICE,
    )
    run = await orch.run_to_completion(run.id, principal=ALICE)
    assert run.state is S.FAILED
    assert run.attempts == 2
    assert "gave up after 2" in (run.last_error or "")
    exhausted = audit.for_action("failed")
    assert exhausted and exhausted[-1].detail.get("exhausted") is True


# ── permanent failure is not retried ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_permanent_failure_is_never_retried(orchestrator, handlers):
    handlers.script["classify"] = StepResult.fail("422 invalid referral payload")
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    run = await orchestrator.run_to_completion(run.id, principal=ALICE)
    assert run.state is S.FAILED
    assert run.attempts == 1, "a permanent failure consumed more than one attempt"
    assert [n for n, _ in handlers.calls] == ["classify"]


@pytest.mark.asyncio
async def test_a_permanent_failure_is_marked_permanent_in_the_audit(orchestrator, handlers, audit):
    handlers.script["classify"] = StepResult.fail("422 invalid payload")
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.run_to_completion(run.id, principal=ALICE)
    failed = audit.for_action("failed")[-1]
    assert failed.detail.get("permanent") is True


# ── needs-review parks rather than fails ─────────────────────────────────────

@pytest.mark.asyncio
async def test_needs_review_parks_the_run_in_exception(orchestrator, handlers):
    handlers.script["classify"] = StepResult.needs_review("two candidate cases matched")
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    run = await orchestrator.run_to_completion(run.id, principal=ALICE)
    assert run.state is S.EXCEPTION
    assert run.last_error == "two candidate cases matched"


@pytest.mark.asyncio
async def test_an_exception_run_is_resumable_after_the_cause_is_fixed(orchestrator, handlers):
    """`exception` is a parking state, not a grave."""
    handlers.script["classify"] = StepResult.needs_review("ambiguous")
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    run = await orchestrator.run_to_completion(run.id, principal=ALICE)
    assert run.state is S.EXCEPTION

    handlers.script["classify"] = StepResult.ok()     # the cause is addressed
    run = await orchestrator.run_to_completion(run.id, principal=ALICE)
    assert run.state is S.SUCCEEDED


# ── partial failure keeps what succeeded ─────────────────────────────────────

@pytest.mark.asyncio
async def test_a_later_failure_does_not_undo_an_earlier_success(
    orchestrator, handlers, repo
):
    """The record of what did happen has to survive the thing that did not --
    otherwise a resumed run cannot tell which effects it has already caused."""
    handlers.script["enrich"] = StepResult.fail("downstream rejected it")
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    run = await orchestrator.run_to_completion(run.id, principal=ALICE)
    assert run.state is S.FAILED
    steps = {s.name: s for s in await repo.steps_for(run.id)}
    assert steps["classify"].state is S.SUCCEEDED
    assert steps["enrich"].state is S.FAILED


@pytest.mark.asyncio
async def test_resuming_after_a_partial_failure_does_not_repeat_the_done_step(
    orchestrator, handlers
):
    handlers.script["enrich"] = StepResult.needs_review("needs a human")
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.run_to_completion(run.id, principal=ALICE)
    assert [n for n, _ in handlers.calls] == ["classify", "enrich"]

    handlers.script["enrich"] = StepResult.ok()
    run = await orchestrator.run_to_completion(run.id, principal=ALICE)
    assert run.state is S.SUCCEEDED
    # classify must appear exactly once across both passes.
    assert [n for n, _ in handlers.calls].count("classify") == 1


# ── the due-retry sweep ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resume_due_picks_up_a_run_whose_retry_has_come(orchestrator, handlers, repo):
    handlers.script["classify"] = StepResult.retry("transient")
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.advance(run.id, principal=ALICE)

    handlers.script["classify"] = StepResult.ok()
    resumed = await orchestrator.resume_due(principal=ALICE)
    assert [r.id for r in resumed] == [run.id]
    assert (await repo.get_run(run.id)).state is S.SUCCEEDED


@pytest.mark.asyncio
async def test_resume_due_leaves_a_run_whose_retry_is_in_the_future(
    orchestrator, handlers, repo
):
    handlers.script["classify"] = StepResult.retry("transient", retry_after=3600)
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.advance(run.id, principal=ALICE)
    resumed = await orchestrator.resume_due(principal=ALICE)
    assert resumed == []
    assert (await repo.get_run(run.id)).state is S.RETRY_SCHEDULED


@pytest.mark.asyncio
async def test_one_failing_run_does_not_stop_the_sweep(orchestrator, handlers, repo):
    """A poison run must not prevent every other workflow from recovering."""
    handlers.script["classify"] = StepResult.retry("transient")
    first = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    second = await orchestrator.start(
        definition="test.simple", subject_ref="email-2", principal=ALICE,
    )
    await orchestrator.advance(first.id, principal=ALICE)
    await orchestrator.advance(second.id, principal=ALICE)

    calls = {"n": 0}

    def poison_then_fine(ctx):
        calls["n"] += 1
        if ctx.subject_ref == "email-1":
            raise RuntimeError("still broken")
        return StepResult.ok()

    handlers.script["classify"] = poison_then_fine
    await orchestrator.resume_due(principal=ALICE)
    assert (await repo.get_run(second.id)).state is S.SUCCEEDED


# ── recovery from an interrupted worker ──────────────────────────────────────

@pytest.mark.asyncio
async def test_a_lapsed_lease_makes_an_interrupted_run_recoverable(
    orchestrator, repo, handlers
):
    """A killed process leaves a lease that expires, not a lock held forever."""
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    stored = await repo.get_run(run.id)
    stored.state = S.RUNNING
    stored.attempts = 1
    stored.lease_expires_at = utcnow() - timedelta(minutes=1)   # the worker died
    await repo.save_run(stored)

    recovered = await orchestrator.recover_stale(principal=ALICE)
    assert [r.id for r in recovered] == [run.id]
    assert (await repo.get_run(run.id)).state is S.RETRY_SCHEDULED


@pytest.mark.asyncio
async def test_a_live_lease_is_left_alone_by_recovery(orchestrator, repo):
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    stored = await repo.get_run(run.id)
    stored.state = S.RUNNING
    stored.lease_expires_at = utcnow() + timedelta(minutes=5)   # still working
    await repo.save_run(stored)

    assert await orchestrator.recover_stale(principal=ALICE) == []
    assert (await repo.get_run(run.id)).state is S.RUNNING


@pytest.mark.asyncio
async def test_recovery_does_not_refund_the_attempt_already_spent(
    orchestrator, repo
):
    """A step that reliably kills its worker must exhaust its budget and surface,
    rather than being retried forever."""
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    stored = await repo.get_run(run.id)
    stored.state = S.RUNNING
    stored.attempts = 2
    stored.lease_expires_at = utcnow() - timedelta(minutes=1)
    await repo.save_run(stored)

    await orchestrator.recover_stale(principal=ALICE)
    assert (await repo.get_run(run.id)).attempts == 2


@pytest.mark.asyncio
async def test_recovery_is_audited(orchestrator, repo, audit):
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    stored = await repo.get_run(run.id)
    stored.state = S.RUNNING
    stored.lease_expires_at = utcnow() - timedelta(minutes=1)
    await repo.save_run(stored)
    await orchestrator.recover_stale(principal=ALICE)
    assert "recovered_stale" in audit.actions()


# ── failures outside a step handler ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_failure_outside_the_handler_parks_the_run_visibly(
    repo, definitions, handlers, audit
):
    """A run left `running` with no lease is invisible to `recover_stale`, which
    looks for lapsed leases -- so it would sit there forever with nothing to pick
    it up. It parks in `exception` instead: visible, and still resumable."""
    class BrokenContext:
        async def fetch(self, request):
            raise RuntimeError("CRM lookup exploded")

    definitions.register(
        WorkflowDefinition(
            name="test.ctx",
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
    orch = Orchestrator(
        repository=repo, definitions=definitions, handlers=handlers,
        authorization=AllowAll(), audit=audit,
        context_provider=BrokenContext(),
        backoff_base=0.0, backoff_cap=0.0,
    )
    run = await orch.start(
        definition="test.ctx", subject_ref="email-1", principal=ALICE,
    )
    run = await orch.advance(run.id, principal=ALICE)

    assert run.state is S.EXCEPTION
    assert "orchestration_error" in audit.actions()
    stored = await repo.get_run(run.id)
    assert stored.state is S.EXCEPTION
    # No lease held, and not stranded in `running`.
    assert stored.lease_expires_at is None


@pytest.mark.asyncio
async def test_a_run_parked_by_an_orchestration_error_can_be_resumed(
    repo, definitions, handlers, audit
):
    calls = {"n": 0}

    class FlakyContext:
        async def fetch(self, request):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("CRM lookup exploded")
            from platform_orchestration import ContextBundle
            return ContextBundle()

    definitions.register(
        WorkflowDefinition(
            name="test.ctx2",
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
    orch = Orchestrator(
        repository=repo, definitions=definitions, handlers=handlers,
        authorization=AllowAll(), audit=audit,
        context_provider=FlakyContext(),
        backoff_base=0.0, backoff_cap=0.0,
    )
    run = await orch.start(
        definition="test.ctx2", subject_ref="email-1", principal=ALICE,
    )
    run = await orch.advance(run.id, principal=ALICE)
    assert run.state is S.EXCEPTION

    run = await orch.run_to_completion(run.id, principal=ALICE)
    assert run.state is S.SUCCEEDED
