"""The gated outbound-reply workflow, end to end.

This is the integration test for the whole foundation as the mail agent uses it:
real workflow definition, real step handlers, real routing table, the shared
engine, with only the database and the mail transport faked. The property it is
here to demonstrate is the one the ticket turns on -- a reply is not sent until a
human with the approval grant has said so, and when it is sent there is a record
of who said so.

The repository is the in-memory one rather than the SQL one, for the same reason
the rest of this service's suite uses fakes: there is no database driver in the
venv, and the two implementations satisfy the same port (asserted in the
library's own `test_sql_models.py`).
"""

from __future__ import annotations

import pytest

from app import orchestration, send_identity
from app.mailbox_scope import MailboxScope
from platform_orchestration import (
    InMemoryAuditSink,
    InMemoryWorkflowRepository,
    Orchestrator,
    Principal,
    WorkflowState,
)

S = WorkflowState
ALICE = Principal(user_id="alice")
CRON = Principal(user_id="", service=True)


def scope(*, sendable=("mbx-1",), approvable=("mbx-1",), safeguarding=False):
    return MailboxScope(
        user_id="alice",
        readable=frozenset({"mbx-1"}),
        sendable=frozenset(sendable),
        approvable=frozenset(approvable),
        safeguarding=safeguarding,
    )


@pytest.fixture
def sent(monkeypatch):
    """Capture outbound sends instead of performing them.

    Patched at `pipeline._send_fn_for`, which is the single chooser both this
    workflow and the existing manual reply route go through -- so a test that
    sees nothing here is a test where nothing could have gone out by any path.
    """
    calls: list[dict] = []

    def fake_send(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        orchestration.pipeline, "_send_fn_for", lambda provider: fake_send
    )
    return calls


@pytest.fixture
def audit():
    return InMemoryAuditSink()


@pytest.fixture
def orch(patched_sessions, audit):
    """The real mail definitions and handlers on an in-memory store."""
    return Orchestrator(
        repository=InMemoryWorkflowRepository(),
        definitions=orchestration.definitions,
        handlers=orchestration.handlers,
        authorization=orchestration.MailboxAuthorizationPort(scope()),
        audit=audit,
        routing=orchestration.build_routing_table(),
        backoff_base=0.0,
        backoff_cap=0.0,
    )


async def _start(orch, principal=ALICE, email_id="email-1", body=None):
    run = await orch.start(
        definition=orchestration.REPLY_APPROVAL,
        subject_ref=email_id,
        principal=principal,
        payload={"body": body} if body else {},
        idempotency_key=email_id,
    )
    return await orch.run_to_completion(run.id, principal=principal)


@pytest.fixture
def auto_orch():
    """An orchestrator wired as the analysis pipeline wires it: no user scope, a
    service principal, and the autonomous definition."""
    return Orchestrator(
        repository=InMemoryWorkflowRepository(),
        definitions=orchestration.definitions,
        handlers=orchestration.handlers,
        authorization=orchestration.SystemAuthorizationPort(),
        audit=InMemoryAuditSink(),
        routing=orchestration.build_routing_table(),
    )


async def _auto_start(orch, email_id="email-1", body="auto draft"):
    run = await orch.start(
        definition=orchestration.AUTO_REPLY,
        subject_ref=email_id,
        principal=CRON,
        payload={"body": body, "auto": True},
        idempotency_key=email_id,
    )
    return await orch.run_to_completion(run.id, principal=CRON)


# ── the gate ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_started_workflow_stops_at_the_approval_gate(orch, sent):
    run = await _start(orch)
    assert run.state is S.AWAITING_APPROVAL
    assert sent == [], "a reply went out before anyone approved it"


@pytest.mark.asyncio
async def test_the_prepare_step_succeeds_before_the_gate(orch):
    run = await _start(orch)
    steps = {s.name: s for s in await orch.steps(run.id, principal=ALICE)}
    assert steps["prepare"].state is S.SUCCEEDED
    # The send step has not started at all -- it is not merely unfinished.
    assert "send" not in steps


@pytest.mark.asyncio
async def test_sweeping_cannot_push_the_workflow_through_the_gate(orch, sent):
    run = await _start(orch)
    await orch.run_to_completion(run.id, principal=ALICE)
    await orch.advance(run.id, principal=ALICE)
    assert sent == []


# ── approval sends ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_approval_sends_the_reply(orch, sent):
    run = await _start(orch)
    run = await orch.approve_and_continue(run.id, principal=ALICE, approved=True)
    assert run.state is S.SUCCEEDED
    assert len(sent) == 1
    assert sent[0]["to_addr"] == "sender@example.org"
    assert sent[0]["subject"] == "Re: New referral"
    assert sent[0]["body"] == "Thank you for your referral."


@pytest.mark.asyncio
async def test_a_reviewer_s_edited_text_is_what_goes_out(orch, sent):
    run = await _start(orch, body="A carefully edited reply.")
    await orch.approve_and_continue(run.id, principal=ALICE, approved=True)
    assert sent[0]["body"] == "A carefully edited reply."


@pytest.mark.asyncio
async def test_the_email_is_marked_sent_but_not_auto_sent(orch, sent, store):
    """`auto_sent` means the AI sent it unattended. A person approved this one,
    and the review UI reads that column."""
    run = await _start(orch)
    await orch.approve_and_continue(run.id, principal=ALICE, approved=True)
    email = store.rows[("Email", "email-1")]
    assert email.reply_status == "sent"
    assert email.auto_sent is False


@pytest.mark.asyncio
async def test_rejection_sends_nothing(orch, sent):
    run = await _start(orch)
    run = await orch.submit_approval(run.id, principal=ALICE, approved=False)
    assert run.state is S.REJECTED
    assert sent == []


# ── who may approve ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_user_without_the_approve_grant_cannot_send(
    patched_sessions, audit, sent
):
    from platform_orchestration import NotAuthorized
    orch = Orchestrator(
        repository=InMemoryWorkflowRepository(),
        definitions=orchestration.definitions,
        handlers=orchestration.handlers,
        authorization=orchestration.MailboxAuthorizationPort(scope(approvable=())),
        audit=audit,
        routing=orchestration.build_routing_table(),
    )
    run = await _start(orch)
    with pytest.raises(NotAuthorized):
        await orch.approve_and_continue(run.id, principal=ALICE, approved=True)
    assert sent == []


@pytest.mark.asyncio
async def test_the_scheduler_cannot_approve_its_own_work(patched_sessions, audit, sent):
    """Refused twice over: the system port says no, and the engine refuses any
    non-human principal regardless of what a port says."""
    from platform_orchestration import NotAuthorized
    orch = Orchestrator(
        repository=InMemoryWorkflowRepository(),
        definitions=orchestration.definitions,
        handlers=orchestration.handlers,
        authorization=orchestration.SystemAuthorizationPort(),
        audit=audit,
        routing=orchestration.build_routing_table(),
    )
    run = await _start(orch, principal=CRON)
    assert run.state is S.AWAITING_APPROVAL
    with pytest.raises(NotAuthorized):
        await orch.approve_and_continue(run.id, principal=CRON, approved=True)
    assert sent == []


# ── what prepare refuses ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_agent_never_replies_to_restricted_material_on_its_own(
    patched_sessions, store, email_factory, auto_orch, sent
):
    """`send_identity.RESTRICTED_NO_AUTONOMOUS`, enforced in the workflow.

    Whether to respond to a safeguarding disclosure is a clinical decision, so the
    agent does not take it unattended -- not even with the org's auto-reply
    setting switched on.
    """
    store.put(email_factory(restricted=True))
    run = await _auto_start(auto_orch)
    assert run.state is S.FAILED
    assert run.last_error == send_identity.RESTRICTED_NO_AUTONOMOUS
    assert sent == []


@pytest.mark.asyncio
async def test_a_cleared_human_may_still_approve_a_reply_to_restricted_material(
    patched_sessions, store, email_factory, sent
):
    """The gate adds an approval requirement; it does not remove a capability.

    Before this workflow existed, a cleared user could reply to a restricted email
    through the ungated route. That route is now gated, so refusing restricted
    material here as well would have left nobody able to answer a safeguarding
    item at all. Clearance is still required -- it is checked by
    `MailboxAuthorizationPort`, which is what refuses the *uncleared* user in
    `test_workflow_authorization.py`.
    """
    store.put(email_factory(restricted=True))
    orch_cleared = Orchestrator(
        repository=InMemoryWorkflowRepository(),
        definitions=orchestration.definitions,
        handlers=orchestration.handlers,
        authorization=orchestration.MailboxAuthorizationPort(scope(safeguarding=True)),
        audit=InMemoryAuditSink(),
        routing=orchestration.build_routing_table(),
    )
    run = await _start(orch_cleared)
    assert run.state is S.AWAITING_APPROVAL
    assert sent == [], "it still waits for the approval; it is not sent outright"

    run = await orch_cleared.approve_and_continue(
        run.id, principal=ALICE, approved=True, note="cleared lead reviewed"
    )
    assert run.state is S.SUCCEEDED
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_the_agent_will_not_act_on_an_unresolved_receiving_address(
    patched_sessions, store, email_factory, auto_orch, sent
):
    """`RESOLVED_STATUSES`: an exception-state address "must not drive autonomous
    behaviour". Replying from a guessed organisational identity is worse than not
    replying."""
    store.put(email_factory(route="exception"))
    run = await _auto_start(auto_orch)
    assert run.state is S.FAILED
    assert run.last_error == send_identity.UNRESOLVED_RECEIVING_ADDRESS
    assert sent == []


@pytest.mark.asyncio
async def test_a_human_may_reply_to_a_legacy_row_the_agent_may_not(
    patched_sessions, store, email_factory, orch, auto_orch, sent
):
    """Pre-PW rows carry `receiving_address_status="legacy"` -- recipient data was
    never collected. Refusing a human on that basis would make every historical
    email permanently unanswerable, which the gate has no business doing."""
    store.put(email_factory(receiving_address_status="legacy"))

    auto = await _auto_start(auto_orch)
    assert auto.state is S.FAILED
    assert auto.last_error == send_identity.UNRESOLVED_RECEIVING_ADDRESS

    run = await _start(orch)
    assert run.state is S.AWAITING_APPROVAL
    assert sent == []


@pytest.mark.asyncio
async def test_an_alias_that_forbids_replies_is_respected(
    patched_sessions, store, alias_factory, orch, sent
):
    """safeguarding@ is configured this way, and it holds independently of
    clearance -- neither the agent nor a human may send from it."""
    store.alias = alias_factory(reply_allowed=False)
    run = await _start(orch)
    assert run.state is S.FAILED
    assert run.last_error == send_identity.ALIAS_REPLY_FORBIDDEN
    assert sent == []


@pytest.mark.asyncio
async def test_an_email_with_no_draft_is_refused(
    patched_sessions, store, email_factory, orch, sent
):
    store.put(email_factory(draft_reply=None))
    run = await _start(orch)
    assert run.state is S.FAILED
    assert "no reply body" in (run.last_error or "")


@pytest.mark.asyncio
async def test_an_already_sent_email_is_refused(
    patched_sessions, store, email_factory, orch, sent
):
    """Succeeding with nothing to do would let the send step run and double-send."""
    store.put(email_factory(reply_status="sent"))
    run = await _start(orch)
    assert run.state is S.FAILED
    assert "already been sent" in (run.last_error or "")
    assert sent == []


@pytest.mark.asyncio
async def test_a_refusal_is_not_retried(
    patched_sessions, store, email_factory, orch
):
    """Every refusal `prepare` can give is permanent; retrying would only put the
    same question to a reviewer three times."""
    store.put(email_factory(draft_reply=None))
    run = await _start(orch)
    assert run.state is S.FAILED
    assert run.attempts == 1


# ── duplicate prevention ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_starting_twice_for_one_email_yields_one_review_item(orch):
    """A double-clicked button must not produce two things to approve."""
    first = await _start(orch)
    second = await _start(orch)
    assert second.id == first.id


@pytest.mark.asyncio
async def test_approving_twice_does_not_send_twice(orch, sent):
    from platform_orchestration import InvalidTransition
    run = await _start(orch)
    await orch.approve_and_continue(run.id, principal=ALICE, approved=True)
    with pytest.raises(InvalidTransition):
        await orch.approve_and_continue(run.id, principal=ALICE, approved=True)
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_transient_send_failure_is_retried_and_sends_once(
    patched_sessions, monkeypatch, audit
):
    """The failure mode that matters: a retry must not deliver two copies."""
    attempts = {"n": 0}
    delivered: list[dict] = []

    def flaky(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("smtp connection reset")
        delivered.append(kwargs)

    monkeypatch.setattr(
        orchestration.pipeline, "_send_fn_for", lambda provider: flaky
    )
    orch = Orchestrator(
        repository=InMemoryWorkflowRepository(),
        definitions=orchestration.definitions,
        handlers=orchestration.handlers,
        authorization=orchestration.MailboxAuthorizationPort(scope()),
        audit=audit, routing=orchestration.build_routing_table(),
        backoff_base=0.0, backoff_cap=0.0,
    )
    run = await _start(orch)
    run = await orch.approve_and_continue(run.id, principal=ALICE, approved=True)
    assert run.state is S.SUCCEEDED
    assert attempts["n"] == 2
    assert len(delivered) == 1


@pytest.mark.asyncio
async def test_a_send_that_keeps_failing_gives_up_without_sending(
    patched_sessions, monkeypatch, audit
):
    def always_fails(**kwargs):
        raise RuntimeError("mail host unreachable")

    monkeypatch.setattr(
        orchestration.pipeline, "_send_fn_for", lambda provider: always_fails
    )
    orch = Orchestrator(
        repository=InMemoryWorkflowRepository(),
        definitions=orchestration.definitions,
        handlers=orchestration.handlers,
        authorization=orchestration.MailboxAuthorizationPort(scope()),
        audit=audit, routing=orchestration.build_routing_table(),
        backoff_base=0.0, backoff_cap=0.0,
    )
    run = await _start(orch)
    run = await orch.approve_and_continue(run.id, principal=ALICE, approved=True)
    assert run.state is S.FAILED
    assert run.attempts == 3        # the step's declared budget
    assert "gave up after 3" in (run.last_error or "")


# ── audit ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_approval_and_the_send_are_both_audited(orch, audit, sent):
    run = await _start(orch)
    await orch.approve_and_continue(
        run.id, principal=ALICE, approved=True, note="checked",
    )
    actions = audit.actions()
    assert "approval_requested" in actions
    assert "approved" in actions
    assert "step_succeeded" in actions
    approved = audit.for_action("approved")[0]
    assert approved.actor_user_id == "alice"
    assert approved.step == "send"


@pytest.mark.asyncio
async def test_no_message_content_reaches_the_audit_trail(orch, audit, sent):
    """The draft reaches the send step; it must not reach the trail."""
    run = await _start(orch, body="A reply quoting the family's circumstances.")
    await orch.approve_and_continue(run.id, principal=ALICE, approved=True)
    blob = str([e.detail for e in audit.events])
    assert "family's circumstances" not in blob
    assert "[redacted]" in blob


@pytest.mark.asyncio
async def test_the_per_email_audit_trail_records_the_approval(
    patched_sessions, store, sent
):
    """`email_audit` has only ever had "ingested" written to it. The approval and
    the send are exactly the rows the SSOT asks for by name."""
    orch = Orchestrator(
        repository=InMemoryWorkflowRepository(),
        definitions=orchestration.definitions,
        handlers=orchestration.handlers,
        authorization=orchestration.MailboxAuthorizationPort(scope()),
        audit=orchestration.SentAuditMirror(),
        routing=orchestration.build_routing_table(),
    )
    run = await _start(orch)
    await orch.approve_and_continue(run.id, principal=ALICE, approved=True)

    rows = [r for r in store.added_of("EmailAudit")]
    actions = [r.action for r in rows]
    assert "approved" in actions
    assert "sent" in actions
    approved = next(r for r in rows if r.action == "approved")
    assert approved.actor_user_id == "alice"
    assert approved.email_id == "email-1"
    assert approved.mailbox_id == "mbx-1"


@pytest.mark.asyncio
async def test_a_rejection_is_recorded_on_the_email_too(patched_sessions, store):
    orch = Orchestrator(
        repository=InMemoryWorkflowRepository(),
        definitions=orchestration.definitions,
        handlers=orchestration.handlers,
        authorization=orchestration.MailboxAuthorizationPort(scope()),
        audit=orchestration.SentAuditMirror(),
        routing=orchestration.build_routing_table(),
    )
    run = await _start(orch)
    await orch.submit_approval(
        run.id, principal=ALICE, approved=False, note="not appropriate",
    )
    actions = [r.action for r in store.added_of("EmailAudit")]
    assert "rejected" in actions
