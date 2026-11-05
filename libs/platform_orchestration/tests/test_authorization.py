"""Authorization: enforced in the engine, never derived from a caller's input.

The engine's three gates are `can_start`, `can_view` and `can_approve`, and they
are deliberately separate -- in the mail agent these are three different grants,
and a user who may read a mailbox is routinely not the person who may authorise an
action in it.

The other property under test is that an unauthorised *read* is indistinguishable
from a missing run. A 403 confirms the record exists and that somebody else can see
it, which for safeguarding material is itself a disclosure; the same decision the
mail agent already took with `MailboxForbidden`.
"""

from __future__ import annotations

import dataclasses

import pytest

from platform_orchestration import (
    DenyAll,
    NotAuthorized,
    Orchestrator,
    Principal,
    WorkflowNotFound,
    WorkflowState,
)

from .conftest import ALICE, BOB

S = WorkflowState


class ScopedPort:
    """Authorises per subject reference, with the three grants held separately.

    Shaped like the mail agent's real adapter: read, send/start and approve are
    independent sets, and nothing is inferred from one to another.
    """

    def __init__(self, *, viewable=(), startable=(), approvable=()):
        self.viewable = set(viewable)
        self.startable = set(startable)
        self.approvable = set(approvable)

    async def can_start(self, principal, *, capability, subject_ref):
        return subject_ref in self.startable

    async def can_view(self, principal, *, capability, subject_ref):
        return subject_ref in self.viewable

    async def can_approve(self, principal, *, capability, subject_ref):
        return subject_ref in self.approvable


def _orchestrator(repo, definitions, handlers, audit, port):
    return Orchestrator(
        repository=repo, definitions=definitions, handlers=handlers,
        authorization=port, audit=audit, backoff_base=0.0, backoff_cap=0.0,
    )


# ── the default is to refuse ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_default_port_refuses_everything(repo, definitions, handlers, audit):
    """A host that forgets to wire authorization gets a service that does nothing,
    rather than one that authorises everything."""
    orch = _orchestrator(repo, definitions, handlers, audit, DenyAll())
    with pytest.raises(NotAuthorized):
        await orch.start(
            definition="test.simple", subject_ref="email-1", principal=ALICE,
        )


@pytest.mark.asyncio
async def test_an_orchestrator_with_no_port_given_refuses(repo, definitions, handlers, audit):
    orch = Orchestrator(
        repository=repo, definitions=definitions, handlers=handlers, audit=audit,
    )
    with pytest.raises(NotAuthorized):
        await orch.start(
            definition="test.simple", subject_ref="email-1", principal=ALICE,
        )


# ── starting ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_starting_requires_the_start_grant(repo, definitions, handlers, audit):
    port = ScopedPort(viewable={"email-1"}, startable=set())
    orch = _orchestrator(repo, definitions, handlers, audit, port)
    with pytest.raises(NotAuthorized):
        await orch.start(
            definition="test.simple", subject_ref="email-1", principal=ALICE,
        )


@pytest.mark.asyncio
async def test_no_run_is_created_when_starting_is_refused(
    repo, definitions, handlers, audit
):
    port = ScopedPort(viewable={"email-1"})
    orch = _orchestrator(repo, definitions, handlers, audit, port)
    with pytest.raises(NotAuthorized):
        await orch.start(
            definition="test.simple", subject_ref="email-1", principal=ALICE,
        )
    assert await repo.list_runs() == ()


@pytest.mark.asyncio
async def test_a_refused_start_runs_no_handler(repo, definitions, handlers, audit):
    orch = _orchestrator(repo, definitions, handlers, audit, DenyAll())
    with pytest.raises(NotAuthorized):
        await orch.start(
            definition="test.simple", subject_ref="email-1", principal=ALICE,
        )
    assert handlers.calls == []


# ── reading ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_unauthorised_read_looks_like_a_missing_run(
    repo, definitions, handlers, audit
):
    """Not a 403: confirming the run exists is itself a disclosure."""
    port = ScopedPort(viewable={"email-1"}, startable={"email-1"})
    orch = _orchestrator(repo, definitions, handlers, audit, port)
    run = await orch.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )

    port.viewable.clear()        # access revoked
    with pytest.raises(WorkflowNotFound):
        await orch.get(run.id, principal=ALICE)


@pytest.mark.asyncio
async def test_a_genuinely_missing_run_raises_the_same_error(
    repo, definitions, handlers, audit
):
    port = ScopedPort(viewable={"email-1"})
    orch = _orchestrator(repo, definitions, handlers, audit, port)
    with pytest.raises(WorkflowNotFound):
        await orch.get("no-such-run", principal=ALICE)


@pytest.mark.asyncio
async def test_advancing_a_run_you_cannot_see_is_refused(
    repo, definitions, handlers, audit
):
    port = ScopedPort(viewable={"email-1"}, startable={"email-1"})
    orch = _orchestrator(repo, definitions, handlers, audit, port)
    run = await orch.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    port.viewable.clear()
    with pytest.raises(WorkflowNotFound):
        await orch.advance(run.id, principal=BOB)
    assert handlers.calls == []


@pytest.mark.asyncio
async def test_reading_steps_is_authorised_before_anything_is_returned(
    repo, definitions, handlers, audit
):
    port = ScopedPort(viewable={"email-1"}, startable={"email-1"})
    orch = _orchestrator(repo, definitions, handlers, audit, port)
    run = await orch.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    await orch.run_to_completion(run.id, principal=ALICE)
    port.viewable.clear()
    with pytest.raises(WorkflowNotFound):
        await orch.steps(run.id, principal=ALICE)


# ── listing ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_listing_excludes_runs_the_caller_cannot_view(
    repo, definitions, handlers, audit
):
    port = ScopedPort(
        viewable={"email-1"}, startable={"email-1", "email-2"},
    )
    orch = _orchestrator(repo, definitions, handlers, audit, port)
    await orch.start(definition="test.simple", subject_ref="email-1", principal=ALICE)
    await orch.start(definition="test.simple", subject_ref="email-2", principal=ALICE)

    rows = await orch.list_runs(principal=ALICE)
    assert [r.subject_ref for r in rows] == ["email-1"]


@pytest.mark.asyncio
async def test_an_explicit_empty_authorised_set_returns_nothing(
    repo, definitions, handlers, audit
):
    """"Authorised for nothing" must not be read as "no filter" -- the classic
    shape of this bug, and the one the mail agent's own scope code warns about."""
    port = ScopedPort(viewable={"email-1"}, startable={"email-1"})
    orch = _orchestrator(repo, definitions, handlers, audit, port)
    await orch.start(definition="test.simple", subject_ref="email-1", principal=ALICE)

    rows = await orch.list_runs(principal=ALICE, subject_refs=[])
    assert rows == ()


@pytest.mark.asyncio
async def test_a_subject_ref_filter_can_only_narrow(repo, definitions, handlers, audit):
    """Asking for a record you are not entitled to cannot widen the result."""
    port = ScopedPort(viewable={"email-1"}, startable={"email-1", "email-2"})
    orch = _orchestrator(repo, definitions, handlers, audit, port)
    await orch.start(definition="test.simple", subject_ref="email-1", principal=ALICE)
    await orch.start(definition="test.simple", subject_ref="email-2", principal=ALICE)

    rows = await orch.list_runs(principal=ALICE, subject_refs=["email-1"])
    assert [r.subject_ref for r in rows] == ["email-1"]


# ── approving ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_approval_needs_the_approve_grant_not_merely_read(
    repo, definitions, handlers, audit
):
    """Reading a mailbox does not make you the person who may authorise a send."""
    port = ScopedPort(
        viewable={"email-1"}, startable={"email-1"}, approvable=set(),
    )
    orch = _orchestrator(repo, definitions, handlers, audit, port)
    run = await orch.start(
        definition="test.gated", subject_ref="email-1", principal=ALICE,
    )
    run = await orch.run_to_completion(run.id, principal=ALICE)
    assert run.state is S.AWAITING_APPROVAL

    with pytest.raises(NotAuthorized):
        await orch.submit_approval(run.id, principal=ALICE, approved=True)


@pytest.mark.asyncio
async def test_a_refused_approval_leaves_no_decision_and_sends_nothing(
    repo, definitions, handlers, audit
):
    port = ScopedPort(viewable={"email-1"}, startable={"email-1"})
    orch = _orchestrator(repo, definitions, handlers, audit, port)
    run = await orch.start(
        definition="test.gated", subject_ref="email-1", principal=ALICE,
    )
    await orch.run_to_completion(run.id, principal=ALICE)
    with pytest.raises(NotAuthorized):
        await orch.submit_approval(run.id, principal=ALICE, approved=True)

    assert await repo.approval_for(run.id, "send") is None
    assert "send" not in [n for n, _ in handlers.calls]
    assert (await repo.get_run(run.id)).state is S.AWAITING_APPROVAL


@pytest.mark.asyncio
async def test_the_approver_grant_is_enough_to_approve(
    repo, definitions, handlers, audit
):
    port = ScopedPort(
        viewable={"email-1"}, startable={"email-1"}, approvable={"email-1"},
    )
    orch = _orchestrator(repo, definitions, handlers, audit, port)
    run = await orch.start(
        definition="test.gated", subject_ref="email-1", principal=ALICE,
    )
    await orch.run_to_completion(run.id, principal=ALICE)
    run = await orch.approve_and_continue(run.id, principal=ALICE, approved=True)
    assert run.state is S.SUCCEEDED


@pytest.mark.asyncio
async def test_cancelling_requires_the_approve_grant(
    repo, definitions, handlers, audit
):
    port = ScopedPort(viewable={"email-1"}, startable={"email-1"})
    orch = _orchestrator(repo, definitions, handlers, audit, port)
    run = await orch.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    with pytest.raises(NotAuthorized):
        await orch.cancel(run.id, principal=ALICE, reason="no longer needed")


# ── the principal is not taken from caller input ─────────────────────────────

@pytest.mark.asyncio
async def test_the_recorded_starter_is_the_principal_not_the_payload(
    repo, definitions, handlers, audit
):
    """A payload claiming to be somebody else changes nothing: the engine records
    the principal the host built from verified token claims."""
    port = ScopedPort(viewable={"email-1"}, startable={"email-1"})
    orch = _orchestrator(repo, definitions, handlers, audit, port)
    run = await orch.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
        payload={"user_id": "mallory", "started_by": "mallory"},
    )
    assert run.started_by == "alice"


@pytest.mark.asyncio
async def test_the_handler_receives_the_real_principal(
    repo, definitions, handlers, audit
):
    port = ScopedPort(viewable={"email-1"}, startable={"email-1"})
    orch = _orchestrator(repo, definitions, handlers, audit, port)
    seen = {}

    def capture(ctx):
        seen["user_id"] = ctx.principal.user_id
        from platform_orchestration import StepResult
        return StepResult.ok()

    handlers.script["classify"] = capture
    run = await orch.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
        payload={"user_id": "mallory"},
    )
    await orch.advance(run.id, principal=ALICE)
    assert seen["user_id"] == "alice"


def test_a_principal_cannot_be_mutated_after_a_check():
    """Frozen, so a principal cannot be escalated between the check and the action."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        ALICE.user_id = "root"      # type: ignore[misc]


def test_a_service_principal_is_not_a_human():
    assert not Principal(user_id="", service=True).is_human
    assert not Principal(user_id="svc", service=True).is_human
    assert Principal(user_id="alice").is_human


def test_a_principal_with_no_subject_is_not_a_human():
    """An unauthenticated caller must not satisfy a human-review requirement."""
    assert not Principal(user_id="").is_human
