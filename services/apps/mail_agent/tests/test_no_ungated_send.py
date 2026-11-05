"""No reply leaves this service without an approval on record.

Two kinds of test, because "the gate works" and "there is no way round the gate"
are different claims and only the second is what the ticket asks for.

The behavioural tests drive the real route functions and the real pipeline entry
point and assert that nothing reaches the mail transport. The static test is the
one that keeps being true: it reads `app/` and asserts that the only place in
application code that can call a send function is the workflow's own step handler.
A future route that sends directly would pass every behavioural test here and fail
that one.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app import api, orchestration, pipeline, send_identity
from app.mailbox_scope import MailboxScope
from platform_orchestration import (
    InMemoryAuditSink,
    InMemoryWorkflowRepository,
    NotAuthorized,
    WorkflowState,
)

S = WorkflowState
APP = Path(inspect.getsourcefile(api)).parent

CLAIMS = {"sub": "alice", "groups": ["g-coordinator"], "scope": "mail-agent:emails.write"}


def scope(*, sendable=("mbx-1",), approvable=("mbx-1",), safeguarding=False):
    return MailboxScope(
        user_id="alice",
        readable=frozenset({"mbx-1"}),
        sendable=frozenset(sendable),
        approvable=frozenset(approvable),
        safeguarding=safeguarding,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def workflow_store(monkeypatch):
    """One in-memory workflow store shared by every orchestrator a test builds.

    `submit_for_approval`, `auto_send_reply` and `reject_open_run` each construct
    their own orchestrator -- that is the point of them, since each binds a
    different authorization port -- so they have to agree about where runs live or
    a test cannot follow a run from one call to the next.
    """
    repo = InMemoryWorkflowRepository()
    audit = InMemoryAuditSink()
    monkeypatch.setattr(orchestration, "workflow_repository", lambda: repo)
    monkeypatch.setattr(orchestration, "audit_sink", lambda: audit)
    repo.audit = audit  # type: ignore[attr-defined]
    return repo


@pytest.fixture
def sent(monkeypatch):
    """Everything that could put a message on the wire, captured.

    Patched at all three levels -- the chooser the workflow uses, and both
    transport functions underneath it -- so a test that sees nothing here is a
    test where nothing could have gone out by any route.
    """
    calls: list[dict] = []

    def fake_send(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(orchestration.pipeline, "_send_fn_for", lambda provider: fake_send)
    for client in ("graph_client", "sendgrid_client", "mail_client"):
        monkeypatch.setattr(
            getattr(orchestration.pipeline, client), "send_reply", fake_send
        )
    return calls


@pytest.fixture
def seeded(store, email_factory, mailbox_factory, patched_sessions):
    store.put(email_factory())
    store.put(mailbox_factory())
    return store


# ─────────────────────────────────────────────────────────────────────────────
# The route that used to send
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_reply_route_does_not_send(seeded, workflow_store, sent):
    """The gap this ticket closes. `POST /emails/{id}/reply` put a body straight
    on the wire for anyone holding `emails.write` plus `can_send`."""
    session = seeded()
    result = await api.send_reply(
        "email-1", api.ReplyIn(body="Thanks, we will be in touch."),
        claims=CLAIMS, scope=scope(), session=session,
    )

    assert sent == [], "the reply route sent a message with no approval on record"
    assert result["state"] == S.AWAITING_APPROVAL.value
    assert result["runId"]


@pytest.mark.asyncio
async def test_the_reply_route_marks_the_email_as_awaiting_release(
    seeded, workflow_store, sent
):
    """"pending" is distinct from "draft" on purpose: a draft is waiting to be
    looked at, this is waiting to be released."""
    email = seeded.rows[("Email", "email-1")]
    await api.send_reply(
        "email-1", api.ReplyIn(body="Thanks."),
        claims=CLAIMS, scope=scope(), session=seeded(),
    )
    assert email.reply_status == "pending"
    assert email.draft_reply == "Thanks."


@pytest.mark.asyncio
async def test_the_reply_route_is_idempotent(seeded, workflow_store, sent):
    """A double-clicked Send button must not create two review items for one
    message, nor send anything on the second press."""
    first = await api.send_reply(
        "email-1", api.ReplyIn(body="Thanks."),
        claims=CLAIMS, scope=scope(), session=seeded(),
    )
    second = await api.send_reply(
        "email-1", api.ReplyIn(body="Thanks again."),
        claims=CLAIMS, scope=scope(), session=seeded(),
    )
    assert first["runId"] == second["runId"]
    assert sent == []


@pytest.mark.asyncio
async def test_a_refused_reply_is_reported_rather_than_queued(
    store, email_factory, mailbox_factory, patched_sessions, workflow_store, sent
):
    """A reply that can never be sent should say so at the point of asking, not
    sit in a review queue for a person to discover."""
    from fastapi import HTTPException

    store.put(email_factory(reply_status="sent"))
    store.put(mailbox_factory())

    with pytest.raises(HTTPException) as caught:
        await api.send_reply(
            "email-1", api.ReplyIn(body="Thanks."),
            claims=CLAIMS, scope=scope(), session=store(),
        )
    assert caught.value.status_code == 422
    assert sent == []


# ─────────────────────────────────────────────────────────────────────────────
# Approval is a separate grant from sending
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_user_who_may_send_but_not_approve_cannot_release_a_reply(
    seeded, workflow_store, sent
):
    """The substance of making the gate mandatory. Before, `can_send` was enough
    to put mail on the wire; now it is enough to *ask*, and no more."""
    result = await api.send_reply(
        "email-1", api.ReplyIn(body="Thanks."),
        claims=CLAIMS, scope=scope(approvable=()), session=seeded(),
    )

    orch = orchestration.build_orchestrator(
        orchestration.MailboxAuthorizationPort(scope(approvable=()))
    )
    principal = orchestration.principal_from_claims(CLAIMS)
    with pytest.raises(NotAuthorized):
        await orch.approve_and_continue(
            result["runId"], principal=principal, approved=True,
        )
    assert sent == []


@pytest.mark.asyncio
async def test_an_uncleared_user_cannot_queue_a_reply_to_restricted_material(
    store, email_factory, mailbox_factory, patched_sessions, workflow_store, sent
):
    """Clearance is a separate axis from mailbox access, and it gates the queueing
    step as well as the approval -- someone who cannot see an item is in no
    position to start an action on it."""
    store.put(email_factory(restricted=True))
    store.put(mailbox_factory())

    with pytest.raises(NotAuthorized):
        await orchestration.submit_for_approval(
            email_id="email-1", body="Thanks.",
            principal=orchestration.principal_from_claims(CLAIMS),
            scope=scope(safeguarding=False),
        )
    assert sent == []


@pytest.mark.asyncio
async def test_dismissing_a_queued_reply_closes_its_run(seeded, workflow_store, sent):
    """Otherwise the decision is taken on the email screen and the workflow queue
    never hears about it, leaving the run for someone else to approve."""
    result = await api.send_reply(
        "email-1", api.ReplyIn(body="Thanks."),
        claims=CLAIMS, scope=scope(), session=seeded(),
    )
    await api.dismiss_reply(
        "email-1", claims=CLAIMS, scope=scope(), session=seeded(),
    )

    run = await workflow_store.get_run(result["runId"])
    assert run.state is S.REJECTED
    assert sent == []


# ─────────────────────────────────────────────────────────────────────────────
# The autonomous path
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_auto_reply_is_approved_by_the_standing_policy(
    seeded, workflow_store, sent
):
    """Auto-reply still sends -- an administrator switched it on and the UI
    promises it -- but the setting is now recorded as the authority for each send
    rather than the send happening with nothing written down."""
    assert await orchestration.auto_send_reply("email-1", body="Automatic reply.")

    assert len(sent) == 1
    run = await workflow_store.find_by_idempotency_key(
        definition=orchestration.AUTO_REPLY, idempotency_key="email-1"
    )
    approval = await workflow_store.approval_for(run.id, "send")
    assert approval.decided_by == "policy:%s" % orchestration.AUTO_REPLY_POLICY
    assert "approved_by_policy" in workflow_store.audit.actions()


@pytest.mark.asyncio
async def test_an_auto_reply_is_marked_as_sent_by_the_agent(
    seeded, workflow_store, sent
):
    """`auto_sent` is what the review UI reads to label the row, so the two paths
    must set it differently."""
    email = seeded.rows[("Email", "email-1")]
    await orchestration.auto_send_reply("email-1", body="Automatic reply.")
    assert email.reply_status == "sent"
    assert email.auto_sent is True


@pytest.mark.asyncio
async def test_an_approved_reply_is_not_marked_as_sent_by_the_agent(
    seeded, workflow_store, sent
):
    email = seeded.rows[("Email", "email-1")]
    result = await api.send_reply(
        "email-1", api.ReplyIn(body="Thanks."),
        claims=CLAIMS, scope=scope(), session=seeded(),
    )
    orch = orchestration.build_orchestrator(
        orchestration.MailboxAuthorizationPort(scope())
    )
    await orch.approve_and_continue(
        result["runId"],
        principal=orchestration.principal_from_claims(CLAIMS),
        approved=True,
    )
    assert email.reply_status == "sent"
    assert email.auto_sent is False
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_an_auto_reply_does_not_answer_over_a_human_who_is_deciding(
    seeded, workflow_store, sent
):
    """Two separate questions, one message: the reviewer's is the one that counts.
    Sending autonomously here would both duplicate the reply and quietly answer
    something a person was asked."""
    await api.send_reply(
        "email-1", api.ReplyIn(body="Thanks."),
        claims=CLAIMS, scope=scope(), session=seeded(),
    )
    assert await orchestration.auto_send_reply("email-1", body="Automatic.") is False
    assert sent == []


@pytest.mark.asyncio
async def test_a_repeated_auto_reply_sends_once(seeded, workflow_store, sent):
    """The idempotency key is the email id, so a re-analysis of the same message
    cannot send a second copy."""
    assert await orchestration.auto_send_reply("email-1", body="Automatic.")
    await orchestration.auto_send_reply("email-1", body="Automatic.")
    assert len(sent) == 1


ROWS = [("email-1", {"fromEmail": "s@example.org", "subject": "x", "uid": "1"})]


@pytest.mark.asyncio
async def test_the_pipeline_withholds_an_auto_send_when_the_org_has_not_enabled_it(
    patched_pipeline, email_factory, mailbox_factory, workflow_store, sent
):
    """Unchanged behaviour, asserted at the new seam: the org setting is still the
    first thing consulted, and nothing is queued when it is off."""
    email = email_factory(reply_status="none")
    patched_pipeline.put(email)
    patched_pipeline.put(mailbox_factory())

    await pipeline.analyze_and_persist(ROWS, auto_reply_enabled=False)

    assert sent == []
    assert email.reply_status == "draft", "it should land as a draft for a human"


@pytest.mark.asyncio
async def test_the_pipeline_sends_through_the_workflow_when_it_is_enabled(
    patched_pipeline, email_factory, mailbox_factory, workflow_store, sent
):
    """The other half of the same test: with the setting on, the reply does go
    out — and it goes out through a run with an approval recorded against it,
    rather than straight from the batch loop."""
    email = email_factory(reply_status="none")
    patched_pipeline.put(email)
    patched_pipeline.put(mailbox_factory())

    await pipeline.analyze_and_persist(ROWS, auto_reply_enabled=True)

    assert len(sent) == 1
    assert email.reply_status == "sent"
    assert email.auto_sent is True

    run = await workflow_store.find_by_idempotency_key(
        definition=orchestration.AUTO_REPLY, idempotency_key="email-1"
    )
    assert run is not None, "the send did not go through a workflow run"
    approval = await workflow_store.approval_for(run.id, "send")
    assert approval.decided_by == "policy:%s" % orchestration.AUTO_REPLY_POLICY


@pytest.mark.asyncio
async def test_the_pipeline_still_withholds_an_auto_send_for_restricted_material(
    patched_pipeline, email_factory, mailbox_factory, workflow_store, sent
):
    """Two independent refusals cover this — the pipeline's own restricted check
    and the workflow's `restricted_no_autonomous_reply`. Asserted through the
    pipeline so the one the agent actually hits first is the one under test."""
    email = email_factory(reply_status="none", restricted=True)
    patched_pipeline.put(email)
    patched_pipeline.put(mailbox_factory())

    await pipeline.analyze_and_persist(ROWS, auto_reply_enabled=True)

    assert sent == []
    # `not`, not `is False`: column defaults are only applied on flush, so an
    # unflushed row's `auto_sent` is None rather than False. What matters is that
    # nothing marked it as sent by the agent.
    assert not email.auto_sent


# ─────────────────────────────────────────────────────────────────────────────
# The static guarantee
# ─────────────────────────────────────────────────────────────────────────────

#: Who may *call* the transport chooser. One module, because one module is
#: allowed to send: the `mail.send_reply` step handler, which the engine will not
#: invoke without an approval on record.
#:
#: Allowlisting a name per module rather than a whole module matters here.
#: `pipeline.py` has to stay permitted to *define* `_send_fn_for`, and an
#: allowlist of modules would therefore also permit `pipeline.py` to call it --
#: which is precisely the bypass that was closed.
MAY_CALL_SEND_CHOOSER = {"orchestration.py"}

#: Who may name a transport's send function at all. Only `_send_fn_for`'s own
#: body, which lives in `pipeline.py`; everywhere else reaches it through the
#: chooser's return value.
MAY_NAME_A_TRANSPORT = {"pipeline.py"}

#: The transport modules whose `send_reply` puts a message on the wire.
TRANSPORTS = {"graph_client", "sendgrid_client", "mail_client"}


def _modules():
    for path in sorted(APP.glob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def _call_name(node):
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _chooser_calls():
    """Every call to `_send_fn_for`, as (module, line)."""
    for path, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) == "_send_fn_for":
                yield path.name, node.lineno


def _transport_references():
    """Every `<transport>.send_reply`, called or merely named, as (module, line).

    References and not just calls: `_send_fn_for` *returns* these functions rather
    than calling them, so a module that took one and called it through a local
    name would never show up as a call to `send_reply`.
    """
    for path, tree in _modules():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "send_reply"
                and isinstance(node.value, ast.Name)
                and node.value.id in TRANSPORTS
            ):
                yield path.name, node.lineno


def test_only_the_workflow_handler_calls_the_transport_chooser():
    """The guard that outlives these tests.

    Adding a route that sends a reply directly is an easy and plausible mistake --
    it is what the old route did, and what the analysis pipeline did. This fails
    at that commit rather than at the first unapproved send.
    """
    offenders = [
        "%s:%d" % (module, line)
        for module, line in _chooser_calls()
        if module not in MAY_CALL_SEND_CHOOSER
    ]
    assert not offenders, (
        "these call pipeline._send_fn_for outside the approval workflow, so they "
        "can send with no approval record: %s" % offenders
    )


def test_only_the_chooser_names_a_transport_directly():
    """Reaching past `_send_fn_for` to a client's own `send_reply` would sidestep
    the guard above, so that is checked separately."""
    offenders = [
        "%s:%d" % (module, line)
        for module, line in _transport_references()
        if module not in MAY_NAME_A_TRANSPORT
    ]
    assert not offenders, (
        "these name a mail transport's send function directly, bypassing the one "
        "permitted call site: %s" % offenders
    )


def test_the_guards_would_notice():
    """Guards the guards. Both tests above pass trivially if their patterns stop
    matching -- after a rename, or if the chooser were inlined."""
    chooser = list(_chooser_calls())
    assert chooser, "no calls to _send_fn_for found at all - has it been renamed?"
    assert {m for m, _ in chooser} == MAY_CALL_SEND_CHOOSER

    transports = list(_transport_references())
    assert transports, "no transport references found at all - have they moved?"
    assert {m for m, _ in transports} == MAY_NAME_A_TRANSPORT


def test_the_send_step_is_the_only_handler_that_sends():
    """And it is gated in both definitions that use it."""
    for name in (orchestration.REPLY_APPROVAL, orchestration.AUTO_REPLY):
        spec = orchestration.definitions.get(name)
        send = spec.step(spec.index_of("send"))
        assert send.handler == "mail.send_reply"
        assert send.requires_approval, "%s could send without an approval" % name
        assert send.consequential


def test_only_the_autonomous_definition_accepts_a_policy_approval():
    """A run started from the review UI must never be releasable by the cron."""
    human = orchestration.definitions.get(orchestration.REPLY_APPROVAL)
    assert not human.step(human.index_of("send")).policy_approval

    auto = orchestration.definitions.get(orchestration.AUTO_REPLY)
    assert auto.step(auto.index_of("send")).policy_approval


def test_the_refusal_reasons_come_from_send_identity():
    """Rather than a second vocabulary for the same decisions. These strings reach
    the audit trail and the UI, so there must be one set of them."""
    source = Path(inspect.getsourcefile(orchestration)).read_text(encoding="utf-8")
    for reason in (
        send_identity.RESTRICTED_NO_AUTONOMOUS,
        send_identity.UNRESOLVED_RECEIVING_ADDRESS,
        send_identity.NO_MAILBOX,
        send_identity.ALIAS_REPLY_FORBIDDEN,
    ):
        assert "send_identity.%s" % _const_name(reason) in source


def _const_name(value: str) -> str:
    for name in dir(send_identity):
        if name.isupper() and getattr(send_identity, name) == value:
            return name
    raise AssertionError("no send_identity constant for %r" % value)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration faults surface as refusals, not crashes
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_mailbox_with_no_credential_fails_the_step_rather_than_the_run(
    store, email_factory, mailbox_factory, patched_sessions, workflow_store, sent
):
    """mypy found this: `client_secret_enc` is nullable, so a Graph mailbox can
    legitimately have no secret. Letting `None` reach Fernet would raise from
    inside the crypto library and park the run on an unreadable error."""
    mailbox = mailbox_factory()
    mailbox.client_secret_enc = None
    store.put(email_factory())
    store.put(mailbox)

    result = await api.send_reply(
        "email-1", api.ReplyIn(body="Thanks."),
        claims=CLAIMS, scope=scope(), session=store(),
    )
    orch = orchestration.build_orchestrator(
        orchestration.MailboxAuthorizationPort(scope())
    )
    run = await orch.approve_and_continue(
        result["runId"],
        principal=orchestration.principal_from_claims(CLAIMS),
        approved=True,
    )
    assert run.state is S.FAILED
    assert "no client secret" in (run.last_error or "")
    assert sent == []


@pytest.mark.asyncio
async def test_re_analysis_does_not_overwrite_a_queued_reply(
    patched_pipeline, email_factory, mailbox_factory, workflow_store, sent
):
    """A reviewer must not have the text they are approving changed underneath
    them, nor see a queued reply presented as an ordinary draft."""
    email = email_factory(reply_status="pending", draft_reply="Awaiting release.")
    patched_pipeline.put(email)
    patched_pipeline.put(mailbox_factory())

    await pipeline.analyze_and_persist(ROWS, auto_reply_enabled=False)

    assert email.reply_status == "pending"
    assert email.draft_reply == "Awaiting release."
    assert sent == []


# ─────────────────────────────────────────────────────────────────────────────
# Who released the send, and what happens when nobody does
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_human_approving_an_autonomous_run_is_not_recorded_as_an_auto_send(
    seeded, workflow_store, sent
):
    """`auto_sent` means "went out without a person pressing send", so it has to
    follow the authority on record rather than how the run was started.

    This is reachable: if the process dies between starting an autonomous run and
    policy-approving it, the run sits in the review queue and a reviewer can
    release it. Inferring `auto_sent` from the run's payload reported that send as
    the agent's, which is the opposite of what happened.
    """
    email = seeded.rows[("Email", "email-1")]
    orch = orchestration.build_orchestrator(orchestration.SystemAuthorizationPort())
    run = await orch.start(
        definition=orchestration.AUTO_REPLY,
        subject_ref="email-1",
        principal=orchestration.SYSTEM_PRINCIPAL,
        payload={"body": "Automatic.", "auto": True},
        idempotency_key="email-1",
    )
    run = await orch.run_to_completion(
        run.id, principal=orchestration.SYSTEM_PRINCIPAL
    )
    assert run.state is S.AWAITING_APPROVAL, "the policy approval never happened"

    human = orchestration.build_orchestrator(
        orchestration.MailboxAuthorizationPort(scope())
    )
    run = await human.approve_and_continue(
        run.id,
        principal=orchestration.principal_from_claims(CLAIMS),
        approved=True,
        note="reviewer released a stranded auto-reply",
    )

    assert run.state is S.SUCCEEDED
    assert len(sent) == 1
    assert email.auto_sent is False, "a person released this; it is not an auto-send"

    approval = await workflow_store.approval_for(run.id, "send")
    assert approval.decided_by == "alice"


@pytest.mark.asyncio
async def test_a_stranded_autonomous_run_is_released_by_the_sweep(
    seeded, workflow_store, sent
):
    """End-to-end version of the library's recovery test, through the real
    scheduler tick: the run is parked with no approval, and the tick releases it."""
    email = seeded.rows[("Email", "email-1")]
    orch = orchestration.build_orchestrator(orchestration.SystemAuthorizationPort())
    run = await orch.start(
        definition=orchestration.AUTO_REPLY,
        subject_ref="email-1",
        principal=orchestration.SYSTEM_PRINCIPAL,
        payload={"body": "Automatic.", "auto": True},
        idempotency_key="email-1",
    )
    run = await orch.run_to_completion(
        run.id, principal=orchestration.SYSTEM_PRINCIPAL
    )
    assert run.state is S.AWAITING_APPROVAL
    assert sent == []

    from app import scheduler
    await scheduler._workflow_tick()

    assert len(sent) == 1
    assert email.reply_status == "sent"
    assert email.auto_sent is True, "the standing policy released it"
    approval = await workflow_store.approval_for(run.id, "send")
    assert approval.decided_by == "policy:%s" % orchestration.AUTO_REPLY_POLICY


@pytest.mark.asyncio
async def test_the_sweep_will_not_release_a_reply_waiting_for_a_person(
    seeded, workflow_store, sent
):
    """The property that makes the sweep safe to run every two minutes. A
    `mail.reply_approval` run is a person's decision and the tick must not take
    it."""
    result = await api.send_reply(
        "email-1", api.ReplyIn(body="Thanks."),
        claims=CLAIMS, scope=scope(), session=seeded(),
    )

    from app import scheduler
    await scheduler._workflow_tick()

    run = await workflow_store.get_run(result["runId"])
    assert run.state is S.AWAITING_APPROVAL, "the sweep released a human's decision"
    assert sent == []


# ─────────────────────────────────────────────────────────────────────────────
# Three further bypass routes, closed statically
# ─────────────────────────────────────────────────────────────────────────────

#: The endpoints that actually transmit. Each belongs to exactly one module.
TRANSPORT_URLS = {
    "sendMail": "graph_client.py",            # POST /users/{mailbox}/sendMail
    "api.sendgrid.com": "sendgrid_client.py",  # POST /v3/mail/send
    "smtplib": "mail_client.py",
}


def test_no_module_reaches_a_transport_endpoint_directly():
    """`_send_fn_for` can be bypassed by going straight to the wire.

    The other guards watch the chooser and the clients' `send_reply`. Neither
    would notice a new route doing its own `requests.post` to `/sendMail`, which
    needs no import from this service at all. So the endpoints themselves are
    pinned to the one module each belongs to.
    """
    offenders: dict[str, list[str]] = {}
    for path in sorted(APP.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for needle, owner in TRANSPORT_URLS.items():
            if path.name == owner:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                # Comments and docstrings cross-reference these freely; only code
                # is a bypass.
                stripped = line.strip()
                if stripped.startswith("#") or needle not in line:
                    continue
                offenders.setdefault(path.name, []).append("%s:%d" % (needle, i))
    assert not offenders, (
        "these reach a mail transport endpoint outside its own client module: %r"
        % offenders
    )


def test_the_transport_endpoints_are_where_the_guard_expects():
    """Guards the guard: if a client is renamed or an endpoint changes, the test
    above silently stops checking anything."""
    for needle, owner in TRANSPORT_URLS.items():
        text = (APP / owner).read_text(encoding="utf-8")
        assert needle in text, "%s no longer contains %r" % (owner, needle)


#: Names that reach a module or attribute computed at runtime. Every guard above
#: reads source text, so each is blind to `import_module("app." + "graph" + "_c"
#: "lient")` -- the module never appears as a literal and the AST holds no
#: `Import` node to find. Nothing in `app/` needs any of this: there were zero
#: uses of all four when this guard was written, so forbidding them outright
#: costs nothing and removes the whole evasion class rather than one spelling
#: of it.
#:
#: This is a narrow claim. It does not make the service tamper-proof -- anyone
#: editing `orchestration.py` can still send -- it keeps the other guards from
#: being trivially sidesteppable by code that looks innocent.
NO_DYNAMIC_LOOKUP = {"importlib", "__import__", "getattr", "eval"}


def test_no_app_module_reaches_a_transport_by_computed_name():
    """A dynamic import evades every text-matching guard in this file.

    Found by probing the guards rather than the code: a module doing
    `getattr(import_module("app." + "graph" + "_client"), "send" + "_reply")`
    passed all thirty tests while holding a live bypass.
    """
    offenders: dict[str, list[str]] = {}
    for path, tree in _modules():
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                name = node.value.id
            if name in NO_DYNAMIC_LOOKUP:
                offenders.setdefault(path.name, []).append(
                    "%s:%d" % (name, node.lineno)
                )
    assert not offenders, (
        "dynamic module or attribute lookup in app/ would evade the guards "
        "above; resolve the target statically instead: %r" % offenders
    )


def test_the_dynamic_lookup_guard_would_notice():
    """Guards the guard, on the AST the real one walks."""
    source = """
import importlib
m = importlib.import_module('app.' + 'graph' + '_client')
f = getattr(m, 'send' + '_reply')
"""
    tree = ast.parse(source)
    found = {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    } | {
        n.value.id for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
    }
    assert found & NO_DYNAMIC_LOOKUP == {"importlib", "getattr"}


def test_no_route_lets_a_caller_choose_the_workflow():
    """A route taking a definition name from the request would let a caller pick
    which gate applies to them.

    `mail.auto_reply` exists only to be started by the analysis pipeline, and its
    send step accepts a standing policy approval. Nothing reachable over HTTP may
    name a definition; both routes that start one hardcode `REPLY_APPROVAL`.
    """
    from app import workflow_api

    # Stronger than "the routes hardcode the right name": the HTTP layer does not
    # name a definition at all. Both routes go through
    # `orchestration.submit_for_approval`, so there is one place where the choice
    # is made and it is not reachable from a request body.
    for module in (api, workflow_api):
        text = Path(inspect.getsourcefile(module)).read_text(encoding="utf-8")
        offenders = [
            "%s:%d: %s" % (module.__name__, i, line.strip())
            for i, line in enumerate(text.splitlines(), 1)
            if "definition=" in line and not line.strip().startswith("#")
        ]
        assert not offenders, (
            "an HTTP module names a workflow definition, so a route could choose "
            "which gate applies: %s" % offenders
        )

    # And the one place that does choose, chooses the human-gated workflow.
    adapter = Path(inspect.getsourcefile(orchestration)).read_text(encoding="utf-8")
    body = adapter.split("async def submit_for_approval")[1].split("async def")[0]
    assert "definition=REPLY_APPROVAL" in body
    assert "AUTO_REPLY" not in body


def test_the_autonomous_definition_is_only_started_server_side():
    """The other half: `AUTO_REPLY` is named in the adapter and the pipeline's
    entry point, never in a request handler."""
    from app import workflow_api

    for module in (api, workflow_api):
        text = Path(inspect.getsourcefile(module)).read_text(encoding="utf-8")
        assert "AUTO_REPLY" not in text, (
            "%s references the autonomous definition; it must not be reachable "
            "from a request" % module.__name__
        )
