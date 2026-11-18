"""What the agent records about a submission the CRM has not acted on yet.

The CRM stopped creating records when an agent posts to /intake: it holds the
ask until a coordinator approves it. So "we sent it" and "it exists" became
two different facts, and the one thing this agent must never do is claim the
second when only the first is true -- or, worse, claim the first when the
second is already true and there is a real referral nobody is looking for.
"""

from __future__ import annotations

import pytest

from app.crm_sync import _children, _submission_outcome


class TestSubmissionOutcome:
    def test_a_staged_submission_is_not_reported_as_filed(self):
        """The ordinary case. Nothing exists in the CRM yet."""
        status, reference = _submission_outcome(
            {
                "activity_ref": "PW-A-2026-0001",
                "review_status": "PENDING_REVIEW",
                "proposed_action": "REFERRAL",
                "applied": False,
            },
            created_reference=None,
        )
        assert status == "pending_approval"
        # The review item is the only thing there is to point at.
        assert reference == "PW-A-2026-0001"

    def test_an_approved_submission_is_reported_as_filed(self):
        status, reference = _submission_outcome(
            {
                "activity_ref": "PW-A-2026-0001",
                "applied": True,
                "applied_reference": "PW-R-2026-0123",
            },
            created_reference=None,
        )
        assert status == "sent"
        assert reference == "PW-R-2026-0123"

    def test_an_approved_submission_falls_back_to_the_review_item(self):
        """`applied` without a reference still means the record exists."""
        status, reference = _submission_outcome(
            {"activity_ref": "PW-A-2026-0001", "applied": True},
            created_reference=None,
        )
        assert status == "sent"
        assert reference == "PW-A-2026-0001"

    def test_a_crm_from_before_the_gate_is_understood(self):
        """The rollout case, and the reason this is not a one-liner.

        The agents can be deployed before the CRM. An older CRM creates the
        referral outright and answers with its reference and no activity_ref.
        Reading that as "pending_approval" would record a referral that really
        exists as waiting for an approval nobody will ever be asked for, and
        would throw away the only reference to it.
        """
        ack = {
            "reference": "PW-R-2026-0296",
            "id": "3fec35e6-79ff-4584-9798-e14af00ec1cf",
            "status": "NEW",
            "channel": "EMAIL_AGENT",
            "needs_completion": True,
            "duplicate": False,
        }
        status, reference = _submission_outcome(
            ack, created_reference=ack.get("reference")
        )
        assert status == "sent"
        assert reference == "PW-R-2026-0296"

    def test_an_old_communication_ack_keeps_its_case(self):
        """The pre-gate communication ack names no reference of its own, so
        the case it was filed against is what the agent records."""
        status, reference = _submission_outcome(
            {"id": "abc", "case_ref": "PW-2026-0001", "duplicate": False},
            created_reference="PW-2026-0001",
        )
        assert status == "sent"
        assert reference == "PW-2026-0001"

    @pytest.mark.parametrize("applied", [False, None])
    def test_a_falsy_applied_flag_is_still_pending(self, applied):
        status, _ = _submission_outcome(
            {"activity_ref": "PW-A-1", "applied": applied}, created_reference=None
        )
        assert status == "pending_approval"


class TestChildren:
    def test_an_age_nobody_gave_is_named_rather_than_sent_blank(self):
        """The CRM requires a non-empty age. An email that never mentions one
        used to 422 the whole referral at the door and lose a real family."""
        assert _children([{"name": "James", "age": ""}]) == [
            {"name": "James", "age": "unknown"}
        ]

    def test_a_real_age_is_left_alone(self):
        assert _children([{"name": "Ada", "age": "6"}]) == [
            {"name": "Ada", "age": "6"}
        ]

    def test_a_child_with_no_name_is_dropped(self):
        """Nothing the CRM can hold or a coordinator can act on."""
        assert _children([{"name": "  ", "age": "7"}]) == []

    def test_junk_entries_do_not_take_the_referral_down_with_them(self):
        assert _children(["not a dict", None, {"name": "Ada", "age": "6"}]) == [
            {"name": "Ada", "age": "6"}
        ]

    def test_nothing_is_an_empty_list_not_a_null(self):
        """`children` is non-nullable on the CRM's schema."""
        assert _children(None) == []
        assert _children([]) == []

    def test_long_values_are_cut_to_what_the_CRM_accepts(self):
        [child] = _children([{"name": "x" * 200, "age": "y" * 50}])
        assert len(child["name"]) == 120
        assert len(child["age"]) == 20
