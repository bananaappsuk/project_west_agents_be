"""Which CRM endpoint one analysed call reaches, and what it carries.

AC-10's "integration" coverage. `crm_sync.after_call_analysis` is the single
decision point between an analysis and the CRM, and it had no tests: the
existing suite covers how an acknowledgement is *interpreted*
(`test_crm_submission_outcome.py`) but not which endpoint gets called in the
first place, nor what is in the body.

It is also what shadow parity compares, so this is the safety net underneath
Phase 4's parity assertion.

No database, no network: `crm_sync.crm_client` is a recording double in every
test. Every value here is synthetic, and the names are deliberately obvious
fiction.

    .venv/Scripts/python.exe -m pytest tests/test_intent_routing.py -q
"""

from __future__ import annotations

import pytest

from app import crm_sync

pytestmark = pytest.mark.asyncio

CALL = {
    "ext_id": "SYNTH-CALL-1",
    "call_date": "2026-11-20",
    "caller": "Fictional Caller",
    "phone": "01000 000000",
    "agent": "Reception",
    "duration": "4:12",
    "transcript": "SYNTHETIC. No real call.",
}

#: The acknowledgement shape a CRM *with* the review gate returns: staged for a
#: coordinator, no record created yet.
STAGED = {"activity_ref": "PW-A-REVIEW-1", "applied": False, "reference": None,
          "duplicate": False, "missing_fields": [], "needs_completion": False}


class _Crm:
    def __init__(self, *, fail: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    async def _record(self, kind, payload, answer):
        if self.fail:
            raise RuntimeError("CRM unreachable")
        self.calls.append((kind, payload))
        return answer

    async def submit_referral(self, payload):
        return await self._record("referral", payload, dict(STAGED))

    async def submit_communication(self, payload):
        return await self._record("communication", payload, dict(STAGED))

    async def submit_change_request(self, payload):
        return await self._record("change_request", payload, dict(STAGED))

    async def submit_activity(self, payload):
        # Deliberately not gated by `fail`: the activity log is written in every
        # branch including the failure one, and a double that broke it too would
        # hide that.
        self.calls.append(("activity", payload))
        return {"activity_ref": "PW-A-0001", "duplicate": False}

    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]

    def body(self, kind: str) -> dict:
        return next(p for k, p in self.calls if k == kind)


@pytest.fixture
def crm(monkeypatch):
    double = _Crm()
    monkeypatch.setattr(crm_sync, "crm_client", double)
    monkeypatch.setattr(crm_sync.settings, "crm_enabled", True, raising=False)
    return double


def analysis(**overrides) -> dict:
    base = {"intent": "NONE", "summary": "A synthetic summary."}
    base.update(overrides)
    return base


# ── the switch itself ───────────────────────────────────────────────────────
async def test_crm_sync_switched_off_submits_nothing(monkeypatch):
    double = _Crm()
    monkeypatch.setattr(crm_sync, "crm_client", double)
    monkeypatch.setattr(crm_sync.settings, "crm_enabled", False, raising=False)

    status, ref, activity = await crm_sync.after_call_analysis(CALL, analysis(intent="REFERRAL"))
    assert (status, ref, activity) == ("none", None, None)
    assert double.calls == [], "nothing may be sent while CRM sync is off"


@pytest.mark.parametrize("intent", ["NONE", "", "SOMETHING_ELSE", None])
async def test_an_intent_with_nowhere_to_go_submits_nothing_but_still_logs(crm, intent):
    status, ref, _ = await crm_sync.after_call_analysis(CALL, analysis(intent=intent))
    assert status == "none"
    assert ref is None
    assert crm.kinds() == ["activity"], "only the activity record"


# ── REFERRAL ────────────────────────────────────────────────────────────────
async def test_a_referral_reaches_the_referral_endpoint(crm):
    status, ref, activity = await crm_sync.after_call_analysis(
        CALL, analysis(intent="REFERRAL", referrer_name="Invented Referrer")
    )
    assert "referral" in crm.kinds()
    assert status == "pending_approval"
    assert ref == "PW-A-REVIEW-1"
    assert activity == "PW-A-0001"


async def test_the_referral_body_carries_the_identifying_fields(crm):
    await crm_sync.after_call_analysis(CALL, analysis(intent="REFERRAL"))
    body = crm.body("referral")
    assert body["channel"] == "VOICE_AGENT"
    assert body["idempotency_key"] == CALL["ext_id"]
    assert body["referrer_phone"] == CALL["phone"]
    assert body["source_payload"] == {"call_ext_id": CALL["ext_id"], "handled_by": CALL["agent"]}


async def test_a_referral_falls_back_to_the_caller_as_referrer(crm):
    await crm_sync.after_call_analysis(CALL, analysis(intent="REFERRAL"))
    assert crm.body("referral")["referrer_name"] == CALL["caller"]


async def test_an_unrecognised_referrer_type_becomes_self(crm):
    """A bad enum would 422 the whole referral; dropping to SELF keeps it."""
    await crm_sync.after_call_analysis(
        CALL, analysis(intent="REFERRAL", referrer_type="NOT_A_REAL_TYPE")
    )
    assert crm.body("referral")["referrer_type"] == "SELF"


async def test_a_recognised_referrer_type_is_passed_through(crm):
    await crm_sync.after_call_analysis(
        CALL, analysis(intent="REFERRAL", referrer_type="local_authority")
    )
    assert crm.body("referral")["referrer_type"] == "LOCAL_AUTHORITY"


async def test_an_unrecognised_service_becomes_null_rather_than_a_bad_enum(crm):
    await crm_sync.after_call_analysis(
        CALL, analysis(intent="REFERRAL", service_requested="MADE_UP_SERVICE")
    )
    assert crm.body("referral")["service_requested"] is None


async def test_children_is_a_list_and_never_null(crm):
    """Non-nullable on the CRM's schema: an empty list is the correct 'none'."""
    await crm_sync.after_call_analysis(CALL, analysis(intent="REFERRAL", children=None))
    assert crm.body("referral")["children"] == []


async def test_help_needed_falls_back_through_summary_to_a_default(crm):
    await crm_sync.after_call_analysis(
        CALL, analysis(intent="REFERRAL", summary="The caller's own words.")
    )
    assert crm.body("referral")["help_needed"] == "The caller's own words."

    crm.calls.clear()
    await crm_sync.after_call_analysis(CALL, {"intent": "REFERRAL"})
    assert crm.body("referral")["help_needed"] == "Referral request"


# ── CASE_COMMUNICATION ──────────────────────────────────────────────────────
async def test_a_case_communication_with_a_case_reaches_the_communication_endpoint(crm):
    status, _, _ = await crm_sync.after_call_analysis(
        CALL, analysis(intent="CASE_COMMUNICATION", case_ref="PW-C-0001")
    )
    assert "communication" in crm.kinds()
    assert status == "pending_approval"
    body = crm.body("communication")
    assert body["case_ref"] == "PW-C-0001"
    assert body["channel"] == "CALL"
    assert body["direction"] == "INBOUND"
    assert body["category"] == "EXISTING_CASE"
    assert body["external_ref"] == CALL["ext_id"]
    assert body["duration_seconds"] == 252          # 4:12
    assert body["participant_name"] == CALL["caller"]


async def test_a_case_communication_without_a_case_is_skipped(crm):
    """Nothing to attach it to, so nothing is sent -- but it is still logged."""
    status, ref, activity = await crm_sync.after_call_analysis(
        CALL, analysis(intent="CASE_COMMUNICATION")
    )
    assert status == "skipped"
    assert ref is None
    assert crm.kinds() == ["activity"]
    assert activity == "PW-A-0001"


# ── RESCHEDULE / CANCEL ─────────────────────────────────────────────────────
@pytest.mark.parametrize("intent", ["RESCHEDULE", "CANCEL"])
async def test_a_change_request_needs_both_a_case_and_a_session(crm, intent):
    status, _, _ = await crm_sync.after_call_analysis(
        CALL,
        analysis(intent=intent, case_ref="PW-C-0001", meeting_reference="PW-S-0001"),
    )
    assert "change_request" in crm.kinds()
    body = crm.body("change_request")
    assert body["request_type"] == intent
    assert body["case_ref"] == "PW-C-0001"
    assert body["external_ref"] == CALL["ext_id"]
    assert status == "pending_approval"


@pytest.mark.parametrize("intent", ["RESCHEDULE", "CANCEL"])
@pytest.mark.parametrize(
    "missing", [{"case_ref": "PW-C-1"}, {"meeting_reference": "PW-S-1"}, {}]
)
async def test_a_change_request_missing_either_reference_is_skipped(crm, intent, missing):
    """No fallback: the CRM cannot act on a reschedule it cannot locate.

    Deliberately not downgraded to a plain communication -- `submit_communication`
    now 422s a CHANGE_OR_CANCEL category, so a silent downgrade would fail at
    the CRM's door instead of being visible here.
    """
    status, ref, _ = await crm_sync.after_call_analysis(
        CALL, analysis(intent=intent, **missing)
    )
    assert status == "skipped"
    assert ref is None
    assert "change_request" not in crm.kinds()
    assert "communication" not in crm.kinds()


async def test_a_preferred_time_is_forwarded_when_given(crm):
    await crm_sync.after_call_analysis(
        CALL,
        analysis(intent="RESCHEDULE", case_ref="PW-C-1", meeting_reference="PW-S-1",
                 preferred_start_at="2026-12-01T10:00:00Z"),
    )
    assert crm.body("change_request")["preferred_start_at"] == "2026-12-01T10:00:00Z"


async def test_no_preferred_time_key_when_none_was_given(crm):
    """Absent rather than null: the CRM distinguishes 'no preference' from null."""
    await crm_sync.after_call_analysis(
        CALL, analysis(intent="RESCHEDULE", case_ref="PW-C-1", meeting_reference="PW-S-1")
    )
    assert "preferred_start_at" not in crm.body("change_request")


# ── failure handling ────────────────────────────────────────────────────────
async def test_a_crm_failure_is_recorded_and_never_raised(crm, monkeypatch):
    """One row's crm_status is the whole cost of a CRM problem."""
    broken = _Crm(fail=True)
    monkeypatch.setattr(crm_sync, "crm_client", broken)

    status, ref, activity = await crm_sync.after_call_analysis(
        CALL, analysis(intent="REFERRAL")
    )
    assert status == "failed"
    assert ref is None
    assert activity == "PW-A-0001", "the activity record is written even on failure"


async def test_the_activity_record_is_written_in_every_branch(crm):
    for a in (
        analysis(intent="REFERRAL"),
        analysis(intent="CASE_COMMUNICATION", case_ref="PW-C-1"),
        analysis(intent="CASE_COMMUNICATION"),
        analysis(intent="RESCHEDULE", case_ref="PW-C-1", meeting_reference="PW-S-1"),
        analysis(intent="RESCHEDULE"),
        analysis(intent="NONE"),
    ):
        crm.calls.clear()
        _, _, activity = await crm_sync.after_call_analysis(CALL, a)
        assert activity == "PW-A-0001", a["intent"]
        assert "activity" in crm.kinds()


# ── the helpers the payloads depend on ──────────────────────────────────────
async def test_children_drops_anything_the_crm_would_refuse():
    out = crm_sync._children([
        {"name": "Madeup Child", "age": 7},
        {"name": "  ", "age": 4},            # no name: nothing to act on
        {"age": 3},                           # no name at all
        "not a dict",
        {"name": "Second Child"},             # no age
    ])
    assert out == [
        {"name": "Madeup Child", "age": "7"},
        {"name": "Second Child", "age": "unknown"},
    ]


async def test_children_handles_nothing_at_all():
    assert crm_sync._children(None) == []
    assert crm_sync._children([]) == []


async def test_children_caps_the_lengths_the_crm_limits():
    out = crm_sync._children([{"name": "N" * 300, "age": "A" * 50}])
    assert len(out[0]["name"]) == 120
    assert len(out[0]["age"]) == 20


async def test_an_unknown_age_says_so_rather_than_being_blank():
    """A blank age used to 422 a real family's referral at the CRM's door."""
    assert crm_sync._children([{"name": "Child", "age": ""}])[0]["age"] == "unknown"


@pytest.mark.parametrize(
    "value,expected",
    [("4:12", 252), ("0:30", 30), ("1:00:00", 3600), ("10", 10),
     ("", None), (None, None), ("not:a:number", None)],
)
async def test_the_duration_display_string_parses_back_to_seconds(value, expected):
    assert crm_sync._duration_seconds(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("court", "COURT"), ("  SOLICITOR  ", "SOLICITOR"), ("nonsense", None),
     ("", None), (None, None)],
)
async def test_an_enum_is_accepted_only_if_the_crm_knows_it(value, expected):
    assert crm_sync._enum_or_none(value, crm_sync._REFERRER_TYPES) == expected
