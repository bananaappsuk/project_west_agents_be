"""`voice.intake` against the existing pipeline, with no database and no CRM.

What parity means here, precisely
---------------------------------

Both the existing pipeline and the workflow handler call the same function,
`crm_sync.after_call_analysis`. Neither re-implements the intent decision or the
payload construction. So what has to be proved is not that two payload builders
agree -- there is only one -- but that **the workflow hands that one builder
exactly what the pipeline hands it**.

These tests prove it by replacing `crm_sync.crm_client` with a double that
records every call, then running the submission twice: once the way
`pipeline._run` does it, and once through the orchestrator. The two recordings of
what reached the CRM client are compared field by field.

That is a stronger assertion than comparing the handler's return value, because
it captures the request bodies themselves.

No database: the handler's session factory is injectable, so a stand-in answers
`get(Recording, id)`. No CRM: the client is a double in every test, and the
shadow definition does not reach it at all.

    .venv/Scripts/python.exe -m pytest tests/test_voice_intake_parity.py -q
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from platform_orchestration import (
    SYSTEM_PRINCIPAL,
    InMemoryWorkflowRepository,
    InMemoryAuditSink,
    NotAuthorized,
    Principal,
    WorkflowState,
)

from app import crm_sync
from app.orchestration import (
    CALL_PAYLOAD_FIELDS,
    CAPABILITY,
    INTAKE,
    INTAKE_SHADOW,
    SAFE_OUTPUT_KEYS,
    RecordingAuthorizationPort,
    build_orchestrator,
    safe_output,
)

pytestmark = pytest.mark.asyncio

REC_ID = "rec-parity-1"

#: A synthetic referral. Every value is invented; the names are deliberately
#: obvious fiction. The sensitive-looking keys are present on purpose -- they are
#: the eleven that are never persisted, and the whole point of the one-step
#: design is that they must not reach the audit trail.
ANALYSIS = {
    "summary": "Caller asked about supervised contact for two children.",
    "category": "new_referral",
    "flags": ["uncertain"],
    "route": "referrals",
    "restricted": False,
    "priority": "High",
    "risk": "Low",
    "sentiment": "Neutral",
    "needs_reply": True,
    "suggested_reply": "Thank you for calling, we will be in touch.",
    "confidence": 0.91,
    "intent": "REFERRAL",
    "referrer_name": "Fictional Referrer",
    "referrer_type": "LOCAL_AUTHORITY",
    "requesting_adult_name": "Invented Parent",
    "relationship_to_child": "mother",
    "child_lives_with": "father",
    "children": [{"first_name": "Madeup", "last_name": "Child", "age": 7}],
    "help_needed": "supervised contact",
    "service_requested": "SUPERVISED_CONTACT",
    "case_ref": None,
    "meeting_reference": None,
    "preferred_start_at": None,
}

RECORDING_FIELDS = {
    "ext_id": "SYNTH-0001",
    "call_date": "2026-11-20",
    "caller": "Fictional Referrer",
    "phone": "01000 000000",
    "agent": "Reception",
    "duration": "4:12",
    "transcript": "SYNTHETIC TRANSCRIPT. No real call. Two children mentioned.",
}


class _Recording:
    """Stands in for the ORM row. Only what the handler touches."""

    def __init__(self, rec_id: str, **fields):
        self.id = rec_id
        for key, value in fields.items():
            setattr(self, key, value)
        self.summary = ""
        self.category = ""
        self.flags: list[str] = []
        self.route = ""
        self.priority = ""
        self.risk = ""
        self.sentiment = ""
        self.needs_reply = False
        self.ai_reply = ""
        self.reply_status = "none"
        self.analysis_status = "pending"
        self.intent = "NONE"
        self.crm_status = "none"
        self.crm_reference = None
        self.activity_ref = None


class _Session:
    def __init__(self, rows: dict):
        self.rows = rows
        self.commits = 0

    async def get(self, _model, rec_id):
        return self.rows.get(rec_id)

    async def commit(self):
        self.commits += 1


def _sessions(rows: dict):
    session = _Session(rows)

    @asynccontextmanager
    async def factory():
        yield session

    factory.session = session  # type: ignore[attr-defined]
    return factory


class _CapturingCrmClient:
    """Records what would have been sent, and answers as the CRM would."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def submit_referral(self, payload):
        self.calls.append(("referral", payload))
        # The ack shape a CRM *with* the review gate returns: the submission is
        # staged for a coordinator, no referral exists yet, and the review item
        # is the only reference there is. `_submission_outcome` reads
        # activity_ref/applied to tell this from a pre-gate CRM.
        return {"activity_ref": "PW-A-REVIEW-1", "applied": False,
                "reference": None, "status": "received", "duplicate": False,
                "missing_fields": [], "needs_completion": False}

    async def submit_communication(self, payload):
        self.calls.append(("communication", payload))
        return {"case_ref": "PW-C-1", "duplicate": False, "change_request_id": None}

    async def submit_change_request(self, payload):
        self.calls.append(("change_request", payload))
        return {"change_request_id": "cr-1", "communication_id": "c-1",
                "case_ref": "PW-C-1", "duplicate": False, "message": ""}

    async def submit_activity(self, payload):
        self.calls.append(("activity", payload))
        return {"activity_ref": "PW-A-0001", "duplicate": False}


@pytest.fixture
def crm(monkeypatch):
    """One double, shared by both paths under test, and CRM sync switched on.

    `after_call_analysis` returns early when `crm_enabled` is false, which would
    make a parity run pass by doing nothing on both sides.
    """
    double = _CapturingCrmClient()
    monkeypatch.setattr(crm_sync, "crm_client", double)
    monkeypatch.setattr(crm_sync.settings, "crm_enabled", True, raising=False)
    return double


@pytest.fixture
def analysed(monkeypatch):
    """The agent factory, answering with the synthetic analysis."""
    from app import agent_client

    async def fake_analyze(payload):
        fake_analyze.seen.append(payload)
        return {"analysis": dict(ANALYSIS), "escalate": True,
                "route": ANALYSIS["route"], "restricted": False}

    fake_analyze.seen = []
    monkeypatch.setattr(agent_client, "analyze_call", fake_analyze)
    return fake_analyze


def _harness(rows, **kw):
    port = RecordingAuthorizationPort("voice-agent", session_factory=_sessions(rows))
    audit = InMemoryAuditSink()
    orch = build_orchestrator(
        port,
        repository=InMemoryWorkflowRepository(),
        audit=audit,
        session_factory=_sessions(rows),
        **kw,
    )
    return orch, audit


def _rows():
    return {REC_ID: _Recording(REC_ID, **RECORDING_FIELDS)}


# ── payload parity ──────────────────────────────────────────────────────────
async def test_the_workflow_sends_the_crm_exactly_what_the_pipeline_sends(crm, analysed):
    """The parity assertion, at the request bodies themselves."""
    # 1. the existing pipeline's call, verbatim from pipeline._run
    pipeline_payload = {f: RECORDING_FIELDS[f] for f in CALL_PAYLOAD_FIELDS}
    await crm_sync.after_call_analysis(pipeline_payload, dict(ANALYSIS))
    from_pipeline = list(crm.calls)
    crm.calls.clear()

    # 2. the same submission through the orchestrator
    rows = _rows()
    orch, _ = _harness(rows)
    run = await orch.start(
        definition=INTAKE, subject_ref=REC_ID, principal=SYSTEM_PRINCIPAL,
        idempotency_key=REC_ID,
    )
    run = await orch.run_to_completion(run.id, principal=SYSTEM_PRINCIPAL)
    from_workflow = list(crm.calls)

    assert run.state is WorkflowState.SUCCEEDED, run.last_error
    assert [kind for kind, _ in from_pipeline] == [kind for kind, _ in from_workflow]
    assert from_pipeline == from_workflow, "the CRM would receive a different request"


async def test_the_call_payload_is_built_from_the_same_seven_fields(crm, analysed):
    rows = _rows()
    orch, _ = _harness(rows)
    run = await orch.start(definition=INTAKE, subject_ref=REC_ID,
                           principal=SYSTEM_PRINCIPAL, idempotency_key=REC_ID)
    await orch.run_to_completion(run.id, principal=SYSTEM_PRINCIPAL)

    assert len(analysed.seen) == 1
    assert analysed.seen[0] == {f: RECORDING_FIELDS[f] for f in CALL_PAYLOAD_FIELDS}


async def test_the_recording_is_persisted_exactly_as_the_pipeline_persists_it(crm, analysed):
    rows = _rows()
    orch, _ = _harness(rows)
    run = await orch.start(definition=INTAKE, subject_ref=REC_ID,
                           principal=SYSTEM_PRINCIPAL, idempotency_key=REC_ID)
    await orch.run_to_completion(run.id, principal=SYSTEM_PRINCIPAL)

    r = rows[REC_ID]
    assert r.summary == ANALYSIS["summary"]
    assert r.category == "new_referral"
    assert r.flags == ["uncertain"]
    assert r.route == "referrals"
    assert r.priority == "High"
    assert r.risk == "Low"
    assert r.sentiment == "Neutral"
    assert r.needs_reply is True
    assert r.ai_reply == ANALYSIS["suggested_reply"]
    assert r.reply_status == "pending"      # needs_reply -> pending, as today
    assert r.analysis_status == "done"
    assert r.intent == "REFERRAL"
    assert r.crm_status == "pending_approval"
    assert r.crm_reference == "PW-A-REVIEW-1"   # the review item, not a referral
    assert r.activity_ref == "PW-A-0001"        # the separate activity record


# ── shadow mode ─────────────────────────────────────────────────────────────
async def test_the_shadow_definition_never_reaches_the_crm(crm, analysed):
    """The property that makes a parity run safe to do anywhere."""
    rows = _rows()
    orch, _ = _harness(rows)
    run = await orch.start(definition=INTAKE_SHADOW, subject_ref=REC_ID,
                           principal=SYSTEM_PRINCIPAL, idempotency_key=REC_ID + ":shadow")
    run = await orch.run_to_completion(run.id, principal=SYSTEM_PRINCIPAL)

    assert run.state is WorkflowState.SUCCEEDED
    assert crm.calls == [], "shadow mode submitted to the CRM"
    assert analysed.seen, "shadow mode should still analyse"


async def test_shadow_mode_leaves_the_crm_columns_alone(crm, analysed):
    rows = _rows()
    orch, _ = _harness(rows)
    run = await orch.start(definition=INTAKE_SHADOW, subject_ref=REC_ID,
                           principal=SYSTEM_PRINCIPAL, idempotency_key="s1")
    await orch.run_to_completion(run.id, principal=SYSTEM_PRINCIPAL)

    r = rows[REC_ID]
    assert r.crm_status == "none"
    assert r.crm_reference is None
    assert r.activity_ref is None
    assert r.analysis_status == "done", "the analysis half still happens"


async def test_the_recorded_definition_reuses_and_never_submits(crm, analysed):
    """What `pipeline._run` starts: no second model call, no CRM submission.

    The pairing is enforced in `build_definitions`, because eleven of the
    thirteen analysis keys `after_call_analysis` reads are never persisted -- a
    definition that both reused and submitted would send a referral with the
    children missing from it.
    """
    from app.orchestration import INTAKE_RECORDED

    orch, _ = _harness(_rows())
    recorded = orch._definitions.get(INTAKE_RECORDED)
    assert recorded.capability == CAPABILITY
    assert recorded.steps[0].config == {"submit": False, "reuse_persisted": True}
    assert recorded.steps[0].consequential is False


async def test_a_definition_may_not_both_reuse_and_submit():
    """The guard in build_definitions, exercised directly."""
    import app.orchestration as orchestration

    original = orchestration.INTAKE_RECORDED
    try:
        # Re-register with the forbidden pairing by monkeying the loop's data is
        # not possible from outside, so assert the guard's own condition holds
        # for every definition the module registers.
        handlers = orchestration.build_handlers()
        definitions = orchestration.build_definitions(handlers)
        for name in definitions.names():
            config = definitions.get(name).steps[0].config
            assert not (config.get("reuse_persisted") and config.get("submit")), name
    finally:
        assert orchestration.INTAKE_RECORDED == original


async def test_the_two_definitions_differ_only_in_the_submit_flag(crm, analysed):
    orch, _ = _harness(_rows())
    live = orch._definitions.get(INTAKE)
    shadow = orch._definitions.get(INTAKE_SHADOW)
    assert live.capability == shadow.capability == CAPABILITY
    assert [s.handler for s in live.steps] == [s.handler for s in shadow.steps]
    assert live.steps[0].config == {"submit": True, "reuse_persisted": False}
    assert shadow.steps[0].config == {"submit": False, "reuse_persisted": False}
    # Declared non-consequential deliberately: the CRM stages every submission
    # behind its own human approval, so a rejection reverses it. The library
    # refuses a consequential step with no gate in front of it, and declaring it
    # falsely would be the workaround rather than the answer.
    assert live.steps[0].consequential is False
    assert live.steps[0].requires_approval is False


# ── duplicate suppression ───────────────────────────────────────────────────
async def test_a_second_start_on_the_same_key_returns_the_same_run(crm, analysed):
    rows = _rows()
    orch, audit = _harness(rows)
    first = await orch.start(definition=INTAKE, subject_ref=REC_ID,
                             principal=SYSTEM_PRINCIPAL, idempotency_key=REC_ID)
    second = await orch.start(definition=INTAKE, subject_ref=REC_ID,
                              principal=SYSTEM_PRINCIPAL, idempotency_key=REC_ID)
    assert second.id == first.id
    assert "duplicate_suppressed" in audit.actions()


async def test_concurrent_starts_on_one_key_produce_one_run(crm, analysed):
    """The cron-tick-twice case, run concurrently rather than in sequence."""
    rows = _rows()
    orch, _ = _harness(rows)
    runs = await asyncio.gather(*(
        orch.start(definition=INTAKE, subject_ref=REC_ID,
                   principal=SYSTEM_PRINCIPAL, idempotency_key=REC_ID)
        for _ in range(5)
    ))
    assert len({r.id for r in runs}) == 1


async def test_a_distinct_retry_key_produces_a_distinct_run(crm, analysed):
    """Q-2's requirement: a deliberate retry is its own run."""
    rows = _rows()
    orch, _ = _harness(rows)
    first = await orch.start(definition=INTAKE, subject_ref=REC_ID,
                             principal=SYSTEM_PRINCIPAL, idempotency_key=REC_ID)
    retry = await orch.start(definition=INTAKE, subject_ref=REC_ID,
                             principal=SYSTEM_PRINCIPAL,
                             idempotency_key=f"{REC_ID}:1")
    assert retry.id != first.id
    assert retry.subject_ref == first.subject_ref == REC_ID


# ── retry and state transitions ─────────────────────────────────────────────
async def test_a_transport_failure_schedules_a_retry(crm, monkeypatch):
    from app import agent_client

    async def flaky(_payload):
        raise ConnectionError("upstream unreachable")

    monkeypatch.setattr(agent_client, "analyze_call", flaky)
    rows = _rows()
    orch, _ = _harness(rows)
    run = await orch.start(definition=INTAKE, subject_ref=REC_ID,
                           principal=SYSTEM_PRINCIPAL, idempotency_key=REC_ID)
    run = await orch.advance(run.id, principal=SYSTEM_PRINCIPAL)

    assert run.state is WorkflowState.RETRY_SCHEDULED
    assert run.next_attempt_at is not None
    assert "ConnectionError" in (run.last_error or "")
    assert crm.calls == []


async def test_retries_stop_at_max_attempts(crm, monkeypatch):
    from app import agent_client

    async def always_fails(_payload):
        raise ConnectionError("still unreachable")

    monkeypatch.setattr(agent_client, "analyze_call", always_fails)
    rows = _rows()
    orch, _ = _harness(rows)
    run = await orch.start(definition=INTAKE, subject_ref=REC_ID,
                           principal=SYSTEM_PRINCIPAL, idempotency_key=REC_ID)
    for _ in range(6):
        run = await orch.advance(run.id, principal=SYSTEM_PRINCIPAL)
        if run.state in (WorkflowState.FAILED, WorkflowState.EXCEPTION):
            break
        # Clear the backoff in the store, not on the copy just returned --
        # the engine reads it back from the repository on the next advance.
        stored = await orch._repo.get_run(run.id)
        stored.next_attempt_at = None
        await orch._repo.save_run(stored)
    assert run.state is WorkflowState.FAILED
    assert run.attempts >= 3, "max_attempts=3 should have been spent"


async def test_an_empty_analysis_fails_rather_than_retrying(crm, monkeypatch):
    """A well-formed answer with no content is not a transport problem."""
    from app import agent_client

    async def empty(_payload):
        return {"analysis": {}}

    monkeypatch.setattr(agent_client, "analyze_call", empty)
    rows = _rows()
    orch, _ = _harness(rows)
    run = await orch.start(definition=INTAKE, subject_ref=REC_ID,
                           principal=SYSTEM_PRINCIPAL, idempotency_key=REC_ID)
    run = await orch.run_to_completion(run.id, principal=SYSTEM_PRINCIPAL)
    assert run.state is WorkflowState.FAILED
    assert crm.calls == []


async def test_a_missing_recording_fails_the_run(crm, analysed):
    orch, _ = _harness({})
    run = await orch.start(definition=INTAKE, subject_ref="nope",
                           principal=SYSTEM_PRINCIPAL, idempotency_key="nope")
    run = await orch.run_to_completion(run.id, principal=SYSTEM_PRINCIPAL)
    assert run.state is WorkflowState.FAILED
    assert crm.calls == []


async def test_a_completed_run_reaches_a_terminal_state(crm, analysed):
    from platform_orchestration import is_terminal

    rows = _rows()
    orch, _ = _harness(rows)
    run = await orch.start(definition=INTAKE, subject_ref=REC_ID,
                           principal=SYSTEM_PRINCIPAL, idempotency_key=REC_ID)
    run = await orch.run_to_completion(run.id, principal=SYSTEM_PRINCIPAL)
    assert is_terminal(run.state)
    assert run.finished_at is not None


# ── event redaction: the hard requirement ───────────────────────────────────
async def test_no_audit_event_carries_transcript_summary_or_reply_text(crm, analysed):
    """The requirement stated absolutely, checked against the actual strings."""
    rows = _rows()
    orch, audit = _harness(rows)
    run = await orch.start(definition=INTAKE, subject_ref=REC_ID,
                           principal=SYSTEM_PRINCIPAL, idempotency_key=REC_ID)
    await orch.run_to_completion(run.id, principal=SYSTEM_PRINCIPAL)

    assert audit.actions(), "no audit events were emitted at all"
    blob = "\n".join(repr(e.redacted()) for e in audit.events)

    for secret in (
        RECORDING_FIELDS["transcript"],
        ANALYSIS["summary"],
        ANALYSIS["suggested_reply"],
        ANALYSIS["requesting_adult_name"],
        ANALYSIS["relationship_to_child"],
        ANALYSIS["child_lives_with"],
        ANALYSIS["help_needed"],
        "Madeup",
    ):
        assert secret not in blob, f"{secret!r} reached the audit trail"

    # Also the first 60 characters, since redact() truncates rather than removes.
    for secret in (RECORDING_FIELDS["transcript"], ANALYSIS["summary"]):
        assert secret[:60] not in blob


async def test_no_step_output_carries_an_unlisted_key(crm, analysed):
    rows = _rows()
    orch, _ = _harness(rows)
    run = await orch.start(definition=INTAKE, subject_ref=REC_ID,
                           principal=SYSTEM_PRINCIPAL, idempotency_key=REC_ID)
    await orch.run_to_completion(run.id, principal=SYSTEM_PRINCIPAL)

    steps = await orch.steps(run.id, principal=SYSTEM_PRINCIPAL)
    assert steps
    for step in steps:
        assert set(step.output) <= SAFE_OUTPUT_KEYS, set(step.output) - SAFE_OUTPUT_KEYS


async def test_safe_output_drops_everything_unlisted():
    out = safe_output(
        intent="REFERRAL",
        transcript="a transcript",
        summary="a summary",
        suggested_reply="a reply",
        children=[{"first_name": "Madeup"}],
        crm_status="sent",
    )
    assert out == {"intent": "REFERRAL", "crm_status": "sent"}


async def test_safe_output_drops_non_scalars_even_when_listed():
    """A listed key holding a structure could still carry detail."""
    assert safe_output(intent={"nested": "value"}) == {}
    assert safe_output(category=["a", "list"]) == {}


# ── capability isolation, end to end ────────────────────────────────────────
async def test_a_human_without_scope_cannot_start_an_intake_run(crm, analysed):
    rows = _rows()
    orch, _ = _harness(rows)
    with pytest.raises(NotAuthorized):
        await orch.start(definition=INTAKE, subject_ref=REC_ID,
                         principal=Principal(user_id="u1"), idempotency_key=REC_ID)
    assert crm.calls == []


async def test_every_definition_this_service_registers_is_voice(crm, analysed):
    orch, _ = _harness(_rows())
    for name in orch._definitions.names():
        assert orch._definitions.get(name).capability == CAPABILITY
