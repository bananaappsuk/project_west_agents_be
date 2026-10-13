"""The CRM's own "new referral received" notice must never become a new referral.

The website posts a referral straight to the CRM, which then emails referrals@ to
tell staff. That notice lands in a monitored mailbox quoting the whole submitted
form — referrer, service requested, children, who the child lives with — so a
classifier reading it sees a textbook referral request and says REFERRAL.

Forwarding one back to /intake/referral would create a second record for a family
that already has one. It had not happened yet only because every referral was being
rejected for an unrelated reason (a 152-character idempotency key against a
120-character limit). 9 of the 23 that failed were these notices, so fixing the key
without this guard would have started creating duplicates on the next one.

The PW-R- reference is the tell, and it is decisive: the CRM issued it, so the
referral exists by definition.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# The guard lives in agent_factory, a sibling service whose own package is also
# called `app` — importing it here would collide with mail_agent's. Rather than
# duplicate the pattern (which would then pass while the real one rotted), lift the
# literal out of the file that actually runs in production.
_LLM = (
    # tests -> mail_agent -> apps -> services
    Path(__file__).resolve().parents[3] / "agent_factory" / "app" / "llm.py"
)
_SOURCE = _LLM.read_text(encoding="utf-8")
_MATCH = re.search(r'_REFERRAL_REF_RE = re\.compile\(r"([^"]+)"', _SOURCE)
assert _MATCH, f"_REFERRAL_REF_RE not found in {_LLM} - has it been renamed?"
_REFERRAL_REF_RE = re.compile(_MATCH.group(1), re.IGNORECASE)


def test_the_guard_is_actually_applied():
    """The pattern existing is not enough; it has to be wired into the result."""
    assert 'result.intent == "REFERRAL" and _REFERRAL_REF_RE.search(text)' in _SOURCE


#: A real subject line from the mailbox, and the body the CRM sends with it.
NOTICE_SUBJECT = "New referral received — PW-R-2026-0042 — Supervised Family Time"
NOTICE_BODY = (
    "Referral PW-R-2026-0042\n"
    "Received 06 October 2026 at 16:45 (UK time)\n"
    "Referrer details\n"
    "Full Name: Senthil khan\n"
    "Referral Type: Other family member\n"
    "Service you are referring for: Supervised Family Time\n"
    "Please briefly tell us what help is needed: Family support\n"
    "Child 1: Deepika — age 12\n"
)

#: A genuine request from a member of the public. No reference, because the CRM
#: has never seen this family.
REQUEST = (
    "Referral request — supervised family time for my grandson\n"
    "I would like to make a referral for supervised family time with my grandson, "
    "Tomas Whitfield, aged 6. He currently lives with his mother."
)


def test_the_notice_is_recognised():
    assert _REFERRAL_REF_RE.search(f"{NOTICE_SUBJECT} {NOTICE_BODY}")


def test_a_genuine_request_is_not_caught():
    """The guard must not suppress the referrals the agent exists to forward."""
    assert not _REFERRAL_REF_RE.search(REQUEST)


def test_a_case_reference_is_not_mistaken_for_a_referral_reference():
    """PW-2026-0042 is a case; PW-R-2026-0042 is a referral. Different things."""
    assert not _REFERRAL_REF_RE.search("Regarding case PW-2026-0042, please advise")


@pytest.mark.parametrize(
    "text",
    [
        "referral pw-r-2026-0042 attached",      # lower case
        "Re: Your referral to Project West — PW-R-2026-0043",
        "See PW-R-2026-00431 for details",       # more than four digits
    ],
)
def test_reference_variants(text):
    assert _REFERRAL_REF_RE.search(text)
