"""Receiving-address resolution — SSOT acceptance coverage.

Pure-logic tests: no database, no Graph, no network. The receiving address is the
one piece of evidence that cannot be recovered after collection, so its decision
rules are tested exhaustively rather than sampled.
"""

from __future__ import annotations

import pytest

from app.receiving_address import (
    AMBIGUOUS,
    RESOLVED_PRECEDENCE,
    RESOLVED_SINGLE,
    UNRESOLVED,
    KnownAddress,
    resolve,
)

# The verified live-tenant topology: six physical mailboxes, four business aliases,
# four tenant routing addresses. admin@ is an ALIAS of management@, not a mailbox.
MB = {
    "info": "mb-info",
    "referrals": "mb-referrals",
    "coordinator": "mb-coordinator",
    "finance": "mb-finance",
    "management": "mb-management",
    "reports": "mb-reports",
}

KNOWN = [
    KnownAddress("info@projectwest.co.uk", MB["info"]),
    KnownAddress("referrals@projectwest.co.uk", MB["referrals"]),
    KnownAddress("coordinator@projectwest.co.uk", MB["coordinator"]),
    KnownAddress("finance@projectwest.co.uk", MB["finance"]),
    KnownAddress("management@projectwest.co.uk", MB["management"]),
    KnownAddress("reports@projectwest.co.uk", MB["reports"]),
    KnownAddress("payments@projectwest.co.uk", MB["finance"], is_alias=True,
                 queue="payment_or_finance", reply_allowed=True),
    KnownAddress("complaints@projectwest.co.uk", MB["coordinator"], is_alias=True,
                 queue="complaint", reply_allowed=True),
    KnownAddress("safeguarding@projectwest.co.uk", MB["coordinator"], is_alias=True,
                 queue="safeguarding_restricted", reply_allowed=False, restricted=True),
    KnownAddress("admin@projectwest.co.uk", MB["management"], is_alias=True),
    # Tenant routing addresses: real delivery targets carrying no business meaning.
    KnownAddress("referrals6151@ProjectWest27.onmicrosoft.com", MB["referrals"],
                 is_alias=True, business=False),
    KnownAddress("finance@ProjectWest27.onmicrosoft.com", MB["finance"],
                 is_alias=True, business=False),
]


def to(*addrs):
    return [{"emailAddress": {"address": a}} for a in addrs]


def hdr(**kw):
    return [{"name": k.replace("_", "-"), "value": v} for k, v in kw.items()]


# ── the normal case ───────────────────────────────────────────────────────────

def test_single_mailbox_address_resolves():
    r = resolve(to_recipients=to("referrals@projectwest.co.uk"), cc_recipients=None,
                headers=None, known=KNOWN)
    assert r.status == RESOLVED_SINGLE
    assert r.receiving_address == "referrals@projectwest.co.uk"
    assert r.mailbox_id == MB["referrals"]
    # A mailbox's own address is not an "original recipient" distinct from itself.
    assert r.original_recipient is None
    assert r.allows_autonomous_action


def test_alias_resolves_to_parent_mailbox_and_records_the_alias():
    r = resolve(to_recipients=to("payments@projectwest.co.uk"), cc_recipients=None,
                headers=None, known=KNOWN)
    assert r.status == RESOLVED_SINGLE
    assert r.mailbox_id == MB["finance"], "payments@ physically lands in finance@"
    assert r.original_recipient == "payments@projectwest.co.uk"
    assert r.queue == "payment_or_finance"
    assert r.reply_allowed is True


def test_admin_alias_resolves_to_management_not_a_seventh_mailbox():
    """Access Map v1 said admin@ was a mailbox; the live tenant says alias."""
    r = resolve(to_recipients=to("admin@projectwest.co.uk"), cc_recipients=None,
                headers=None, known=KNOWN)
    assert r.mailbox_id == MB["management"]
    assert r.original_recipient == "admin@projectwest.co.uk"
    assert {k.mailbox_id for k in KNOWN} == set(MB.values()), "exactly six mailboxes"


def test_cc_counts_as_a_receiving_address():
    r = resolve(to_recipients=to("someone@external.example"),
                cc_recipients=to("complaints@projectwest.co.uk"),
                headers=None, known=KNOWN)
    assert r.status == RESOLVED_SINGLE
    assert r.original_recipient == "complaints@projectwest.co.uk"


def test_case_and_whitespace_are_normalised():
    r = resolve(to_recipients=to("  <PAYMENTS@ProjectWest.CO.UK>  "),
                cc_recipients=None, headers=None, known=KNOWN)
    assert r.status == RESOLVED_SINGLE
    assert r.mailbox_id == MB["finance"]


# ── header recovery: the Bcc / distribution-list / redirect cases ────────────

def test_delivered_to_header_recovers_a_bcc_delivery():
    """Bcc leaves nothing in To/Cc; the alias survives only in the headers."""
    r = resolve(to_recipients=to("undisclosed-recipients@example.com"),
                cc_recipients=None,
                headers=hdr(Delivered_To="safeguarding@projectwest.co.uk"),
                known=KNOWN)
    assert r.status == RESOLVED_SINGLE
    assert r.original_recipient == "safeguarding@projectwest.co.uk"


def test_exchange_original_recipient_header_is_used():
    r = resolve(to_recipients=to("list@projectwest.co.uk"), cc_recipients=None,
                headers=[{"name": "X-MS-Exchange-Organization-OriginalRecipient",
                          "value": "complaints@projectwest.co.uk"}],
                known=KNOWN)
    assert r.status == RESOLVED_SINGLE
    assert r.mailbox_id == MB["coordinator"]


def test_comma_separated_header_values_are_split():
    r = resolve(to_recipients=None, cc_recipients=None,
                headers=hdr(Delivered_To="x@external.example, reports@projectwest.co.uk"),
                known=KNOWN)
    assert r.status == RESOLVED_SINGLE
    assert r.mailbox_id == MB["reports"]


# ── exception states: never silently default ────────────────────────────────

def test_no_match_is_unresolved_not_the_polled_mailbox():
    """The whole point. A forward with no known recipient is NOT 'info@'."""
    r = resolve(to_recipients=to("someone@external.example"), cc_recipients=None,
                headers=None, known=KNOWN)
    assert r.status == UNRESOLVED
    assert r.receiving_address is None
    assert r.mailbox_id is None
    assert not r.allows_autonomous_action


def test_two_matches_without_a_precedence_rule_is_ambiguous():
    r = resolve(to_recipients=to("info@projectwest.co.uk", "referrals@projectwest.co.uk"),
                cc_recipients=None, headers=None, known=KNOWN)
    assert r.status == AMBIGUOUS
    assert r.receiving_address is None
    assert set(r.candidates) == {"info@projectwest.co.uk", "referrals@projectwest.co.uk"}
    assert not r.allows_autonomous_action


def test_precedence_rule_resolves_a_multi_match():
    r = resolve(to_recipients=to("info@projectwest.co.uk", "payments@projectwest.co.uk"),
                cc_recipients=None, headers=None, known=KNOWN,
                precedence=["payments@projectwest.co.uk", "info@projectwest.co.uk"])
    assert r.status == RESOLVED_PRECEDENCE
    assert r.receiving_address == "payments@projectwest.co.uk"
    assert r.mailbox_id == MB["finance"]


def test_precedence_rule_that_matches_nothing_leaves_it_ambiguous():
    r = resolve(to_recipients=to("info@projectwest.co.uk", "referrals@projectwest.co.uk"),
                cc_recipients=None, headers=None, known=KNOWN,
                precedence=["finance@projectwest.co.uk"])
    assert r.status == AMBIGUOUS


# ── safeguarding: fail closed ────────────────────────────────────────────────

def test_safeguarding_alias_is_restricted_and_never_auto_actionable():
    r = resolve(to_recipients=to("safeguarding@projectwest.co.uk"), cc_recipients=None,
                headers=None, known=KNOWN)
    assert r.restricted is True
    assert r.reply_allowed is False
    assert r.queue == "safeguarding_restricted"
    # Resolved, but still barred from autonomous action.
    assert r.is_resolved
    assert not r.allows_autonomous_action


def test_ambiguous_match_touching_safeguarding_is_still_restricted():
    """A message that MIGHT have come via safeguarding@ is treated as though it did.

    False positive: a cleared user handles one extra item. False negative: a
    safeguarding disclosure sits in an open queue.
    """
    r = resolve(to_recipients=to("safeguarding@projectwest.co.uk",
                                 "info@projectwest.co.uk"),
                cc_recipients=None, headers=None, known=KNOWN)
    assert r.status == AMBIGUOUS
    assert r.restricted is True
    assert not r.allows_autonomous_action


def test_precedence_cannot_drop_a_restriction_it_outranked():
    """Precedence picks info@, but safeguarding@ also matched — restriction stands."""
    r = resolve(to_recipients=to("safeguarding@projectwest.co.uk",
                                 "info@projectwest.co.uk"),
                cc_recipients=None, headers=None, known=KNOWN,
                precedence=["info@projectwest.co.uk"])
    assert r.status == RESOLVED_PRECEDENCE
    assert r.receiving_address == "info@projectwest.co.uk"
    assert r.restricted is True, "restriction must survive being outranked"
    assert not r.allows_autonomous_action


def test_unresolved_with_restricted_candidate_in_headers_stays_restricted():
    r = resolve(to_recipients=to("x@external.example"), cc_recipients=None,
                headers=hdr(Delivered_To="safeguarding@projectwest.co.uk"),
                known=KNOWN)
    assert r.restricted is True


# ── routing addresses ────────────────────────────────────────────────────────

def test_onmicrosoft_routing_address_resolves_rather_than_excepting():
    """Catalogued so it does not pour noise into the exception queue."""
    r = resolve(to_recipients=to("referrals6151@ProjectWest27.onmicrosoft.com"),
                cc_recipients=None, headers=None, known=KNOWN)
    assert r.status == RESOLVED_SINGLE
    assert r.mailbox_id == MB["referrals"]
    assert r.queue == "", "a routing address implies no business queue"


# ── evidence preservation ────────────────────────────────────────────────────

def test_evidence_is_captured_for_every_outcome():
    r = resolve(to_recipients=to("a@external.example"),
                cc_recipients=to("b@external.example"),
                headers=hdr(Delivered_To="c@external.example"), known=KNOWN)
    assert r.status == UNRESOLVED
    assert r.evidence["to"] == ["a@external.example"]
    assert r.evidence["cc"] == ["b@external.example"]
    assert r.evidence["delivery_headers"] == ["c@external.example"]
    assert r.evidence["candidates"] == []


def test_evidence_records_all_candidates_not_just_the_winner():
    r = resolve(to_recipients=to("info@projectwest.co.uk", "payments@projectwest.co.uk"),
                cc_recipients=None, headers=None, known=KNOWN,
                precedence=["payments@projectwest.co.uk"])
    assert set(r.evidence["candidates"]) == {
        "info@projectwest.co.uk", "payments@projectwest.co.uk"}


# ── degenerate input ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("to_r,cc_r,h", [
    (None, None, None),
    ([], [], []),
    ([{"emailAddress": {}}], None, None),
    ([{}], None, [{"name": "Delivered-To"}]),
])
def test_missing_or_malformed_input_is_unresolved_not_a_crash(to_r, cc_r, h):
    r = resolve(to_recipients=to_r, cc_recipients=cc_r, headers=h, known=KNOWN)
    assert r.status == UNRESOLVED
    assert not r.allows_autonomous_action


# ── Received header `for` clause — the real Bcc/DL recovery path ─────────────
# Verified against the live tenant: Exchange Online does not stamp Delivered-To or
# X-MS-Exchange-Organization-OriginalRecipient here, but Received IS present and its
# `for` clause names the envelope recipient.

def rcvd(*values):
    return [{"name": "Received", "value": v} for v in values]


def test_received_for_clause_recovers_the_delivery_address():
    r = resolve(to_recipients=to("someone-else@external.example"), cc_recipients=None,
                headers=rcvd("from mail.example (1.2.3.4) by pw.example with SMTP id x"
                             " for <safeguarding@projectwest.co.uk>; Tue, 7 Oct 2026"),
                known=KNOWN)
    assert r.status == RESOLVED_SINGLE
    assert r.original_recipient == "safeguarding@projectwest.co.uk"
    assert r.restricted is True


def test_received_for_without_angle_brackets():
    r = resolve(to_recipients=None, cc_recipients=None,
                headers=rcvd("by pw.example with SMTP id q for payments@projectwest.co.uk;"),
                known=KNOWN)
    assert r.status == RESOLVED_SINGLE
    assert r.mailbox_id == MB["finance"]


def test_all_received_hops_are_scanned_not_just_the_first():
    r = resolve(to_recipients=None, cc_recipients=None,
                headers=rcvd("by relay1.example with SMTP id a;",
                             "by relay2.example with SMTP id b for <reports@projectwest.co.uk>;"),
                known=KNOWN)
    assert r.status == RESOLVED_SINGLE
    assert r.mailbox_id == MB["reports"]


def test_received_for_an_external_address_is_ignored():
    """A `for` naming a non-PW address must not resolve anything."""
    r = resolve(to_recipients=None, cc_recipients=None,
                headers=rcvd("by mx.example with SMTP id z for <bob@external.example>;"),
                known=KNOWN)
    assert r.status == UNRESOLVED


def test_received_without_a_for_clause_is_harmless():
    r = resolve(to_recipients=to("info@projectwest.co.uk"), cc_recipients=None,
                headers=rcvd("from a.example by b.example; Tue, 7 Oct 2026 10:00:00 +0000"),
                known=KNOWN)
    assert r.status == RESOLVED_SINGLE
    assert r.receiving_address == "info@projectwest.co.uk"


def test_to_recipients_still_win_when_both_present_and_agree():
    r = resolve(to_recipients=to("complaints@projectwest.co.uk"), cc_recipients=None,
                headers=rcvd("by pw.example for <complaints@projectwest.co.uk>;"),
                known=KNOWN)
    assert r.status == RESOLVED_SINGLE
    assert r.candidates == ("complaints@projectwest.co.uk",), "deduplicated"


def test_received_for_is_recorded_in_evidence():
    r = resolve(to_recipients=None, cc_recipients=None,
                headers=rcvd("by pw.example for <payments@projectwest.co.uk>;"),
                known=KNOWN)
    assert r.evidence["delivery_headers"] == ["payments@projectwest.co.uk"]


# ── most-specific-alias-wins, within one mailbox only ────────────────────────
# Seen repeatedly in the live tenant: a message names both an alias and the mailbox
# it routes into. One destination described twice, not two business functions.

def test_alias_beats_its_own_parent_mailbox():
    r = resolve(to_recipients=to("coordinator@projectwest.co.uk",
                                 "complaints@projectwest.co.uk"),
                cc_recipients=None, headers=None, known=KNOWN)
    assert r.status == RESOLVED_PRECEDENCE
    assert r.receiving_address == "complaints@projectwest.co.uk"
    assert r.queue == "complaint"


def test_safeguarding_alias_beats_coordinator_and_keeps_restriction():
    """The single most important message type in the system."""
    r = resolve(to_recipients=to("coordinator@projectwest.co.uk",
                                 "safeguarding@projectwest.co.uk"),
                cc_recipients=None, headers=None, known=KNOWN)
    assert r.status == RESOLVED_PRECEDENCE
    assert r.receiving_address == "safeguarding@projectwest.co.uk"
    assert r.restricted is True
    assert r.reply_allowed is False
    assert not r.allows_autonomous_action


def test_two_different_mailboxes_stay_ambiguous():
    """coordinator@ + finance@ is a real conflict — a human decides."""
    r = resolve(to_recipients=to("coordinator@projectwest.co.uk",
                                 "finance@projectwest.co.uk"),
                cc_recipients=None, headers=None, known=KNOWN)
    assert r.status == AMBIGUOUS


def test_alias_and_a_different_mailbox_stay_ambiguous():
    """payments@ routes to finance@, info@ is another mailbox — not the same place."""
    r = resolve(to_recipients=to("info@projectwest.co.uk",
                                 "payments@projectwest.co.uk"),
                cc_recipients=None, headers=None, known=KNOWN)
    assert r.status == AMBIGUOUS


def test_all_hands_to_five_mailboxes_stays_ambiguous():
    r = resolve(to_recipients=to("coordinator@projectwest.co.uk",
                                 "finance@projectwest.co.uk",
                                 "management@projectwest.co.uk",
                                 "referrals@projectwest.co.uk",
                                 "reports@projectwest.co.uk"),
                cc_recipients=None, headers=None, known=KNOWN)
    assert r.status == AMBIGUOUS


def test_two_business_aliases_on_one_mailbox_stay_ambiguous():
    """complaints@ and safeguarding@ both land in coordinator@ — genuinely unclear."""
    r = resolve(to_recipients=to("complaints@projectwest.co.uk",
                                 "safeguarding@projectwest.co.uk"),
                cc_recipients=None, headers=None, known=KNOWN)
    assert r.status == AMBIGUOUS
    assert r.restricted is True, "fail closed while unclear"


def test_routing_address_loses_to_the_mailbox_primary_address():
    """Same destination written two ways — resolve to the business address."""
    r = resolve(to_recipients=to("referrals@projectwest.co.uk",
                                 "referrals6151@ProjectWest27.onmicrosoft.com"),
                cc_recipients=None, headers=None, known=KNOWN)
    assert r.status == RESOLVED_PRECEDENCE
    assert r.receiving_address == "referrals@projectwest.co.uk"
    assert r.original_recipient is None, "a routing address is not a business alias"


def test_business_alias_still_beats_a_routing_address_on_the_same_mailbox():
    r = resolve(to_recipients=to("payments@projectwest.co.uk",
                                 "finance@ProjectWest27.onmicrosoft.com"),
                cc_recipients=None, headers=None, known=KNOWN)
    assert r.status == RESOLVED_PRECEDENCE
    assert r.receiving_address == "payments@projectwest.co.uk"
