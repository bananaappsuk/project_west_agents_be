"""Audit events: enough to investigate a failure, nothing sensitive in them.

Two halves. The redaction tests assert that content cannot reach an event even
when a handler puts it there -- the mechanical guarantee, rather than trusting
every future handler author to remember. The event-generation tests assert that
the things an investigator actually needs (who decided, which step, which
transition, why) are present.
"""

from __future__ import annotations

import pytest

from platform_orchestration import (
    AuditEvent,
    FanOutAuditSink,
    InMemoryAuditSink,
    StepResult,
    WorkflowState,
    redact,
)

from .conftest import ALICE

S = WorkflowState


# ── redaction ────────────────────────────────────────────────────────────────

def test_an_email_body_is_redacted():
    assert redact({"body": "a safeguarding disclosure"})["body"] == "[redacted]"


def test_a_draft_reply_is_redacted():
    out = redact({"suggested_reply": "Dear parent...", "draft_reply": "..."})
    assert out["suggested_reply"] == "[redacted]"
    assert out["draft_reply"] == "[redacted]"


def test_a_transcript_is_redacted():
    assert redact({"transcript": "caller said..."})["transcript"] == "[redacted]"


def test_credentials_are_redacted():
    out = redact({
        "smtp_password": "x", "graph_client_secret": "y",
        "api_key": "z", "access_token": "t", "private_key_pem": "p",
    })
    assert set(out.values()) == {"[redacted]"}


def test_a_composite_key_is_caught_by_fragment_matching():
    """`smtp_password` and `email_body` are caught without enumerating every
    composite a capability might invent."""
    out = redact({"smtp_password": "x", "email_body": "y", "user_secret_note": "z"})
    assert out == {
        "smtp_password": "[redacted]",
        "email_body": "[redacted]",
        "user_secret_note": "[redacted]",
    }


def test_redaction_is_case_insensitive():
    assert redact({"BODY": "x", "Client_Secret": "y"}) == {
        "BODY": "[redacted]", "Client_Secret": "[redacted]",
    }


def test_nested_content_is_redacted():
    out = redact({"email": {"subject": "Referral", "body": "content"}})
    assert out["email"]["subject"] == "Referral"
    assert out["email"]["body"] == "[redacted]"


def test_content_inside_a_list_is_redacted():
    out = redact({"items": [{"body": "a"}, {"body": "b"}]})
    assert [i["body"] for i in out["items"]] == ["[redacted]", "[redacted]"]


def test_a_redacted_key_is_present_rather_than_dropped():
    """"Set and not shown" and "never set" are different facts when
    reconstructing what happened."""
    out = redact({"body": "x"})
    assert "body" in out


def test_safe_fields_survive_intact():
    out = redact({
        "category": "new_referral", "confidence": 0.91,
        "restricted": True, "queue": "referrals", "missing": None,
    })
    assert out == {
        "category": "new_referral", "confidence": 0.91,
        "restricted": True, "queue": "referrals", "missing": None,
    }


def test_a_long_string_is_truncated():
    """Keeps one pathological error from filling the audit table, and keeps a
    quoted fragment of a message out of an exception string."""
    out = redact({"error": "e" * 5000})
    assert len(out["error"]) < 600
    assert out["error"].endswith("[truncated]")


def test_a_long_list_is_bounded():
    out = redact({"candidates": list(range(100))})
    assert len(out["candidates"]) == 21
    assert out["candidates"][-1] == "[+80 more]"


def test_deep_nesting_is_cut_off():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": "too far"}}}}}}
    assert "truncated" in str(redact(deep))


def test_an_arbitrary_object_is_reduced_to_its_type_name():
    """An ORM object's repr can carry column values, so it is never trusted."""
    class Email:
        def __repr__(self):
            return "Email(body='a disclosure')"

    out = redact({"row": Email()})
    assert out["row"] == "<Email>"
    assert "disclosure" not in str(out)


def test_an_event_redacts_itself_on_the_way_out():
    event = AuditEvent(
        run_id="r1", action="step_succeeded", detail={"body": "x", "category": "y"},
    )
    out = event.redacted()
    assert out.detail == {"body": "[redacted]", "category": "y"}


# ── event generation ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_creation_is_audited(orchestrator, audit):
    await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    created = audit.for_action("created")
    assert len(created) == 1
    assert created[0].subject_ref == "email-1"
    assert created[0].actor_user_id == "alice"
    assert created[0].detail["definition"] == "test.simple"


@pytest.mark.asyncio
async def test_a_full_run_produces_a_followable_trail(orchestrator, audit):
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.run_to_completion(run.id, principal=ALICE)
    assert audit.actions() == [
        "created",
        "step_started", "step_succeeded",
        "step_started", "step_succeeded",
        "succeeded",
    ]


@pytest.mark.asyncio
async def test_each_event_names_its_step(orchestrator, audit):
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.run_to_completion(run.id, principal=ALICE)
    started = [e.step for e in audit.for_action("step_started")]
    assert started == ["classify", "enrich"]


@pytest.mark.asyncio
async def test_transitions_record_both_ends(orchestrator, audit):
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.run_to_completion(run.id, principal=ALICE)
    done = audit.for_action("succeeded")[0]
    assert done.from_state == "running"
    assert done.to_state == "succeeded"


@pytest.mark.asyncio
async def test_an_approval_records_who_decided(orchestrator, audit):
    run = await orchestrator.start(
        definition="test.gated", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.run_to_completion(run.id, principal=ALICE)
    await orchestrator.submit_approval(
        run.id, principal=ALICE, approved=True, note="fine to send",
    )
    approved = audit.for_action("approved")[0]
    assert approved.actor_user_id == "alice"
    assert approved.actor_kind == "user"
    assert approved.step == "send"


@pytest.mark.asyncio
async def test_a_rejection_is_audited_with_its_step(orchestrator, audit):
    run = await orchestrator.start(
        definition="test.gated", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.run_to_completion(run.id, principal=ALICE)
    await orchestrator.submit_approval(run.id, principal=ALICE, approved=False)
    rejected = audit.for_action("rejected")[0]
    assert rejected.step == "send"
    assert rejected.to_state == "rejected"


@pytest.mark.asyncio
async def test_an_approval_request_is_audited_so_a_review_queue_is_reconstructable(
    orchestrator, audit
):
    run = await orchestrator.start(
        definition="test.gated", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.run_to_completion(run.id, principal=ALICE)
    requested = audit.for_action("approval_requested")[0]
    assert requested.step == "send"
    assert requested.detail["consequential"] is True


@pytest.mark.asyncio
async def test_a_retry_records_the_attempt_and_the_delay(orchestrator, handlers, audit):
    handlers.script["classify"] = StepResult.retry("CRM restarting")
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.advance(run.id, principal=ALICE)
    scheduled = audit.for_action("retry_scheduled")[0]
    assert scheduled.detail["attempt"] == 1
    assert scheduled.detail["error"] == "CRM restarting"
    assert "retry_in_seconds" in scheduled.detail


@pytest.mark.asyncio
async def test_handler_output_is_audited_but_its_content_is_not(
    orchestrator, handlers, audit
):
    """A handler returning a draft is fine -- the draft reaches the next step and
    never the audit trail."""
    handlers.script["classify"] = StepResult.ok(
        category="new_referral", suggested_reply="Dear parent, thank you...",
    )
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.advance(run.id, principal=ALICE)
    event = audit.for_action("step_succeeded")[0]
    assert event.detail["output"]["category"] == "new_referral"
    assert event.detail["output"]["suggested_reply"] == "[redacted]"


@pytest.mark.asyncio
async def test_the_handler_still_receives_the_unredacted_output(orchestrator, handlers):
    """Redaction is for the audit trail, not for the workflow's own data flow."""
    handlers.script["classify"] = StepResult.ok(suggested_reply="Dear parent")
    seen = {}

    def capture(ctx):
        seen.update(dict(ctx.output_of("classify")))
        return StepResult.ok()

    handlers.script["enrich"] = capture
    run = await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=ALICE,
    )
    await orchestrator.run_to_completion(run.id, principal=ALICE)
    assert seen["suggested_reply"] == "Dear parent"


@pytest.mark.asyncio
async def test_a_system_actor_is_distinguished_from_a_user(orchestrator, audit):
    from platform_orchestration import SYSTEM_PRINCIPAL
    await orchestrator.start(
        definition="test.simple", subject_ref="email-1", principal=SYSTEM_PRINCIPAL,
    )
    created = audit.for_action("created")[0]
    assert created.actor_kind == "service"
    assert created.actor_user_id is None


# ── sink behaviour ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_failing_sink_does_not_take_the_workflow_down():
    """An audit destination being unreachable is a reason to shout, not a reason
    to abandon a run that has already had side effects."""
    class Broken:
        async def record(self, event):
            raise RuntimeError("audit store unreachable")

    good = InMemoryAuditSink()
    fan = FanOutAuditSink([Broken(), good])
    await fan.record(AuditEvent(run_id="r1", action="created"))
    assert good.actions() == ["created"]


@pytest.mark.asyncio
async def test_fan_out_reaches_every_sink():
    a, b = InMemoryAuditSink(), InMemoryAuditSink()
    await FanOutAuditSink([a, b]).record(AuditEvent(run_id="r1", action="created"))
    assert a.actions() == b.actions() == ["created"]
