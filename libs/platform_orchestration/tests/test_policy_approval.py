"""Standing policy approvals: a recorded authority for an autonomous action.

Some capabilities are deliberately configured to act unattended -- the mail
agent's auto-reply setting is one. The choice this mechanism exists to settle is
not "human approval or policy approval"; it is "policy approval or *no record at
all*", because the alternative it replaced was a send that happened with nothing
written down about what authorised it.

So these tests are mostly about what it refuses. It must not become a way around
the human gate, which means three things have to hold: a person cannot route their
decision through it, a service principal cannot use it on a step that did not opt
in, and the opt-in cannot be set on a step that has no gate to begin with.
"""

from __future__ import annotations

import pytest

from platform_orchestration import (
    AllowAll,
    DefinitionError,
    DefinitionRegistry,
    InvalidTransition,
    NotAuthorized,
    OrchestrationError,
    Orchestrator,
    Principal,
    StepResult,
    StepSpec,
    WorkflowDefinition,
    WorkflowState,
)

from .conftest import ALICE, CRON

S = WorkflowState
POLICY = "test.autonomous_enabled"


async def _gated(orchestrator, definition="test.policy_gated", principal=CRON):
    run = await orchestrator.start(
        definition=definition, subject_ref="subject-1", principal=principal,
    )
    return await orchestrator.run_to_completion(run.id, principal=principal)


# ── it is still a gate ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_opted_in_step_still_waits(orchestrator, handlers):
    """Opting in does not make the step run unattended by itself. Something has
    to approve it, and until then the handler is not called."""
    run = await _gated(orchestrator)
    assert run.state is S.AWAITING_APPROVAL
    assert ("send", 1) not in handlers.calls


@pytest.mark.asyncio
async def test_a_policy_approval_releases_the_step(orchestrator, handlers):
    run = await _gated(orchestrator)
    run = await orchestrator.policy_approve_and_continue(
        run.id, principal=CRON, policy=POLICY,
    )
    assert run.state is S.SUCCEEDED
    assert ("send", 1) in handlers.calls


@pytest.mark.asyncio
async def test_the_approval_names_the_policy_and_not_a_person(orchestrator, repo):
    """The whole value of the record is that it cannot be mistaken for a human
    decision later. `policy:` is reserved by the engine for exactly that."""
    run = await _gated(orchestrator)
    await orchestrator.submit_policy_approval(run.id, principal=CRON, policy=POLICY)

    approval = await repo.approval_for(run.id, "send")
    assert approval is not None
    assert approval.decision == "approved"
    assert approval.decided_by == "policy:%s" % POLICY


@pytest.mark.asyncio
async def test_the_audit_trail_distinguishes_it_from_a_human_approval(
    orchestrator, audit
):
    run = await _gated(orchestrator)
    await orchestrator.submit_policy_approval(run.id, principal=CRON, policy=POLICY)

    assert "approved_by_policy" in audit.actions()
    assert "approved" not in audit.actions(), (
        "a policy approval reported as a plain approval would make "
        "'who approved this?' unanswerable"
    )
    event = audit.for_action("approved_by_policy")[0]
    assert event.detail["policy"] == POLICY
    assert event.detail["consequential"] is True
    assert event.actor_kind == "service"


# ── what it refuses ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_human_cannot_approve_through_the_policy_path(orchestrator):
    """A person's decision is theirs and is recorded as theirs. Letting a human
    call this would launder an individual's approval into an anonymous one."""
    run = await _gated(orchestrator)
    with pytest.raises(NotAuthorized):
        await orchestrator.submit_policy_approval(
            run.id, principal=ALICE, policy=POLICY,
        )
    assert (await orchestrator.get(run.id, principal=ALICE)).state is S.AWAITING_APPROVAL


@pytest.mark.asyncio
async def test_a_policy_cannot_approve_a_step_that_did_not_opt_in(orchestrator, handlers):
    """The guarantee that adding this method loosened nothing: `test.gated` is the
    ordinary human-gated definition, and it is unreachable this way."""
    run = await _gated(orchestrator, definition="test.gated")
    assert run.state is S.AWAITING_APPROVAL

    with pytest.raises(NotAuthorized):
        await orchestrator.submit_policy_approval(
            run.id, principal=CRON, policy=POLICY,
        )
    assert ("send", 1) not in handlers.calls


@pytest.mark.asyncio
async def test_a_service_principal_still_cannot_use_the_human_path(orchestrator):
    """The existing rule, asserted here too: opting a step in must not have made
    `submit_approval` reachable by a service principal."""
    run = await _gated(orchestrator)
    with pytest.raises(NotAuthorized):
        await orchestrator.submit_approval(run.id, principal=CRON, approved=True)


@pytest.mark.asyncio
async def test_a_policy_approval_must_name_its_policy(orchestrator):
    """An unnamed authority is not an authority. Refused before the run is even
    looked up."""
    run = await _gated(orchestrator)
    with pytest.raises(OrchestrationError):
        await orchestrator.submit_policy_approval(run.id, principal=CRON, policy="")


@pytest.mark.asyncio
async def test_a_policy_cannot_approve_a_run_that_is_not_waiting(orchestrator):
    run = await _gated(orchestrator)
    await orchestrator.policy_approve_and_continue(
        run.id, principal=CRON, policy=POLICY,
    )
    with pytest.raises(InvalidTransition):
        await orchestrator.submit_policy_approval(
            run.id, principal=CRON, policy=POLICY,
        )


@pytest.mark.asyncio
async def test_the_host_can_still_refuse_the_subject(repo, definitions, handlers, audit):
    """Opting a step in says a policy *may* approve it, not that any caller may.
    The authorization port is asked either way."""

    class DenyStart:
        async def can_view(self, principal, *, capability, subject_ref):
            return True

        async def can_start(self, principal, *, capability, subject_ref):
            return False

        async def can_approve(self, principal, *, capability, subject_ref):
            return False

    permissive = Orchestrator(
        repository=repo, definitions=definitions, handlers=handlers,
        authorization=AllowAll(), audit=audit, backoff_base=0.0, backoff_cap=0.0,
    )
    run = await _gated(permissive)

    restricted = Orchestrator(
        repository=repo, definitions=definitions, handlers=handlers,
        authorization=DenyStart(), audit=audit, backoff_base=0.0, backoff_cap=0.0,
    )
    with pytest.raises(NotAuthorized):
        await restricted.submit_policy_approval(
            run.id, principal=CRON, policy=POLICY,
        )


# ── the opt-in cannot be set where there is no gate ──────────────────────────

def test_policy_approval_without_an_approval_gate_is_refused():
    """Otherwise the flag reads as a protection that is not there: a step with
    `policy_approval` and no `requires_approval` never stops at all."""
    with pytest.raises(DefinitionError):
        StepSpec(name="send", handler="test.send", policy_approval=True)


def test_a_consequential_policy_gated_step_must_still_be_gated():
    """The central guarantee, unchanged by the addition: a definition cannot
    declare an ungated consequential step by reaching for the policy flag."""
    registry = DefinitionRegistry()
    with pytest.raises(DefinitionError):
        registry.register(
            WorkflowDefinition(
                name="test.ungated",
                capability="test",
                steps=(
                    StepSpec(name="send", handler="test.send", consequential=True),
                ),
            ),
        )


def test_the_default_is_a_human_or_nothing():
    """Every step written before this flag existed keeps its old meaning."""
    assert StepSpec(name="s", handler="h").policy_approval is False
    assert StepSpec(
        name="s", handler="h", requires_approval=True
    ).policy_approval is False


def test_a_principal_with_no_identity_is_not_treated_as_human():
    """`is_human` is what the two paths split on, so it is pinned here: a service
    principal must not satisfy it, and a person must."""
    assert CRON.is_human is False
    assert ALICE.is_human is True
    assert Principal(user_id="", service=False).is_human is False


# ── recovery of a run stranded at a policy gate ──────────────────────────────
#
# The gap these cover: `resume_due` looks at `retry_scheduled` and
# `recover_stale` at `running` with a lapsed lease. A run parked in
# `awaiting_approval` is outside both, deliberately, because a run waiting for a
# person must wait as long as it takes. A run waiting for a *policy* is not in
# that position -- the authority already exists -- so without a sweep of its own
# it is stranded by any process that dies between starting it and approving it.

@pytest.mark.asyncio
async def test_neither_existing_sweep_touches_a_run_parked_at_a_gate(orchestrator):
    """Stated as a test because it is the reason the third sweep exists, and
    because it is the behaviour that must not change for *human* gates."""
    run = await _gated(orchestrator)
    assert run.state is S.AWAITING_APPROVAL

    assert await orchestrator.resume_due(principal=CRON) == []
    assert await orchestrator.recover_stale(principal=CRON) == []
    assert (await orchestrator.get(run.id, principal=CRON)).state is S.AWAITING_APPROVAL


@pytest.mark.asyncio
async def test_a_stranded_policy_gated_run_is_released_by_the_sweep(
    orchestrator, handlers
):
    """Simulates the process dying between `start` and the policy approval: the
    run exists, is parked, and has no approval recorded."""
    run = await _gated(orchestrator)
    assert ("send", 1) not in handlers.calls, "nothing ran before the approval"

    released = await orchestrator.resume_policy_gated(policy=POLICY, principal=CRON)

    assert [r.id for r in released] == [run.id]
    assert released[0].state is S.SUCCEEDED
    assert ("send", 1) in handlers.calls


@pytest.mark.asyncio
async def test_the_sweep_records_the_policy_as_the_authority(orchestrator, repo, audit):
    run = await _gated(orchestrator)
    await orchestrator.resume_policy_gated(policy=POLICY, principal=CRON)

    approval = await repo.approval_for(run.id, "send")
    assert approval.decided_by == "policy:%s" % POLICY
    assert "approved_by_policy" in audit.actions()
    assert "approved" not in audit.actions()


@pytest.mark.asyncio
async def test_the_sweep_leaves_a_human_gate_alone(orchestrator, handlers):
    """The property that makes the sweep safe to run on a schedule. `test.gated`
    is the ordinary human-gated definition and must sit there untouched."""
    run = await _gated(orchestrator, definition="test.gated")
    assert run.state is S.AWAITING_APPROVAL

    assert await orchestrator.resume_policy_gated(policy=POLICY, principal=CRON) == []

    assert (await orchestrator.get(run.id, principal=ALICE)).state is S.AWAITING_APPROVAL
    assert ("send", 1) not in handlers.calls


@pytest.mark.asyncio
async def test_the_sweep_can_be_narrowed_to_one_definition(orchestrator):
    """A host with more than one policy-gated workflow sweeps them separately,
    because the policy that authorises each is not the same policy."""
    run = await _gated(orchestrator)

    assert await orchestrator.resume_policy_gated(
        policy=POLICY, principal=CRON, definition="test.gated",
    ) == []
    assert (await orchestrator.get(run.id, principal=CRON)).state is S.AWAITING_APPROVAL

    released = await orchestrator.resume_policy_gated(
        policy=POLICY, principal=CRON, definition="test.policy_gated",
    )
    assert [r.id for r in released] == [run.id]


@pytest.mark.asyncio
async def test_one_broken_run_does_not_stop_the_sweep(orchestrator, handlers):
    """Same principle as the other two sweeps: a poison run must not prevent
    every other workflow from recovering.

    Note what "broken" does here. A handler that *raises* is caught by the engine
    and put through the step's ordinary retry budget, so it never propagates out
    of the sweep at all -- the containment is one layer lower than this test was
    originally written to check. Both runs are therefore released; what separates
    them is the state they reach.
    """
    broken = await _gated(orchestrator)
    healthy = await orchestrator.start(
        definition="test.policy_gated", subject_ref="subject-2", principal=CRON,
    )
    healthy = await orchestrator.run_to_completion(healthy.id, principal=CRON)
    assert healthy.state is S.AWAITING_APPROVAL

    def explode_for_subject_one(ctx):
        if ctx.subject_ref == "subject-1":
            raise RuntimeError("this step is broken")
        return StepResult.ok()

    handlers.script["send"] = explode_for_subject_one

    released = await orchestrator.resume_policy_gated(policy=POLICY, principal=CRON)

    by_id = {r.id: r for r in released}
    assert set(by_id) == {broken.id, healthy.id}, "the sweep stopped early"
    assert by_id[healthy.id].state is S.SUCCEEDED
    assert by_id[broken.id].state is not S.SUCCEEDED
    # Both were authorised either way: the policy approval is recorded before
    # anything acts on it, which is what makes a failed send investigable.
    assert by_id[broken.id].state in (S.FAILED, S.RETRY_SCHEDULED)
