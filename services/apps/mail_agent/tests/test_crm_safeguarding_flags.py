"""Safeguarding flags must reach the CRM, and must never break the referral.

The flags are not just recorded: on conversion they set the case's risk level.
abduction_risk, violence_or_aggression and child_abuse_neglect convert to HIGH,
any other flag to MEDIUM, and none at all to LOW. Every referral sent before this
converted at LOW — including the fifteen our own classifier had already marked as
a safeguarding concern — because the field was never populated.

The CRM rejects an unrecognised value with a 422 rather than ignoring it, which is
the right call on their side (an unknown flag would never match their high-risk
set, so a real abduction risk would file as routine). It does mean one bad value
from the model would lose the entire referral, so we validate rather than trust.
"""

from __future__ import annotations

import pytest

from app.crm_sync import _referral_ref_in, _safeguarding_flags


class TestFlags:
    def test_known_values_pass_through(self):
        assert _safeguarding_flags(["domestic_abuse", "mental_health"]) == [
            "domestic_abuse",
            "mental_health",
        ]

    @pytest.mark.parametrize(
        "given",
        ["Domestic Abuse", "domestic-abuse", "  DOMESTIC_ABUSE  ", "Domestic abuse"],
    )
    def test_casing_and_separators_are_normalised(self, given):
        assert _safeguarding_flags([given]) == ["domestic_abuse"]

    def test_an_unknown_value_is_dropped_not_sent(self):
        """One bad value would 422 the whole referral. Lose the flag, keep the referral."""
        assert _safeguarding_flags(["domestic_abuse", "witchcraft"]) == ["domestic_abuse"]

    def test_all_unknown_gives_an_empty_list_not_none(self):
        """The CRM's field is a non-nullable list; [] is the correct 'none'."""
        assert _safeguarding_flags(["nonsense"]) == []
        assert _safeguarding_flags(None) == []
        assert _safeguarding_flags([]) == []

    def test_output_is_deduplicated_and_stable(self):
        """The same email must produce the same payload on a retry."""
        got = _safeguarding_flags(["mental_health", "Mental Health", "domestic_abuse"])
        assert got == ["domestic_abuse", "mental_health"]

    def test_every_high_risk_value_survives(self):
        """These three are what make a case HIGH. A typo here downgrades real risk."""
        high = ["abduction_risk", "violence_or_aggression", "child_abuse_neglect"]
        assert _safeguarding_flags(high) == sorted(high)


class TestReferralReference:
    """Part 2 returns and bounces both quote a reference the CRM already issued."""

    def test_found_in_a_subject(self):
        assert (
            _referral_ref_in("Re: Your referral to Project West — PW-R-2026-0043", None)
            == "PW-R-2026-0043"
        )

    def test_found_in_a_bounce(self):
        assert (
            _referral_ref_in("Undeliverable: Your referral — PW-R-2026-0040", None)
            == "PW-R-2026-0040"
        )

    def test_falls_through_to_the_body(self):
        assert _referral_ref_in("No reference here", "see pw-r-2026-0099") == "PW-R-2026-0099"

    def test_a_case_reference_is_not_a_referral_reference(self):
        """PW-2026-0042 is a case. Sending it here would mislabel the activity."""
        assert _referral_ref_in("About case PW-2026-0042", None) is None

    def test_nothing_quoted_gives_none(self):
        assert _referral_ref_in("Do you cover the Dunfield area?", "no refs") is None
