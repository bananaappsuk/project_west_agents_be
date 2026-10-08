"""Sync eligibility and budget — the first-sync safety gate.

info@ holds 161,833 items. A mailbox with no ingestion boundary must be skipped
with a recorded reason, never defaulted to "beginning of time".
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.mailbox_registry import (
    SKIP_DISABLED,
    SKIP_NO_GRAPH_ID,
    SKIP_NOT_PW,
    SKIP_UNBOUNDED,
    alias_for,
    batch_cap,
    evaluate,
)
from app.receiving_address import KnownAddress

JAN = datetime(2026, 1, 1, tzinfo=timezone.utc)
JUN = datetime(2026, 6, 1, tzinfo=timezone.utc)


def mb(**kw):
    base = dict(mailbox_id="mb1", address="info@projectwest.co.uk", kind="pw",
                enabled=True, graph_user_id="9609fee8", monitor_from=JAN)
    base.update(kw)
    return evaluate(**base)


# ── eligibility ──────────────────────────────────────────────────────────────

def test_configured_pw_mailbox_is_eligible():
    c = mb()
    assert c.eligible
    assert c.skip_reason is None


def test_first_sync_with_no_bound_at_all_is_skipped():
    """No date floor AND no count ceiling = an unbounded walk. Refuse."""
    c = mb(monitor_from=None, first_sync_cap=None, sync_cursor=None)
    assert not c.eligible
    assert c.skip_reason == SKIP_UNBOUNDED


def test_a_count_cap_alone_is_a_sufficient_bound():
    """Project West seeds with the newest 10 and never backfills — no date needed."""
    c = mb(monitor_from=None, first_sync_cap=10, sync_cursor=None)
    assert c.eligible, "first_sync_cap bounds the first sync on its own"


def test_a_date_floor_alone_is_a_sufficient_bound():
    c = mb(monitor_from=JAN, first_sync_cap=None, sync_cursor=None)
    assert c.eligible


def test_an_established_cursor_needs_no_first_sync_bound():
    """Once walking forward from a known point the sync is inherently bounded."""
    c = mb(monitor_from=None, first_sync_cap=None, sync_cursor=JUN)
    assert c.eligible
    assert c.since == JUN


def test_disabled_mailbox_is_skipped():
    assert mb(enabled=False).skip_reason == SKIP_DISABLED


def test_legacy_mailbox_is_never_polled():
    c = mb(kind="legacy", monitor_from=JAN)
    assert not c.eligible
    assert c.skip_reason == SKIP_NOT_PW


def test_mailbox_without_graph_id_is_skipped():
    assert mb(graph_user_id=None).skip_reason == SKIP_NO_GRAPH_ID


def test_skip_precedence_reports_the_most_fundamental_reason():
    """A legacy, disabled, boundary-less mailbox is reported as legacy first."""
    c = mb(kind="legacy", enabled=False, monitor_from=None, graph_user_id=None,
           first_sync_cap=None)
    assert c.skip_reason == SKIP_NOT_PW


# ── where a run starts ───────────────────────────────────────────────────────

def test_first_sync_starts_at_the_boundary():
    assert mb(sync_cursor=None).since == JAN


def test_subsequent_sync_resumes_from_the_cursor():
    assert mb(sync_cursor=JUN).since == JUN


def test_eligible_mailbox_with_a_date_floor_has_a_start_point():
    c = mb()
    assert c.eligible and c.since is not None


def test_cap_only_mailbox_has_no_start_point_and_that_is_fine():
    """Newest-N takes the top of the mailbox; there is nothing to start *from*."""
    c = mb(monitor_from=None, first_sync_cap=10)
    assert c.eligible
    assert c.since is None


# ── per-mailbox budget ───────────────────────────────────────────────────────

def test_per_mailbox_cap_wins_over_a_larger_global_value():
    """info@ must not be able to consume the whole run budget."""
    assert batch_cap(mb(first_sync_cap=500), 10_000) == 500


def test_global_value_wins_when_it_is_the_smaller():
    assert batch_cap(mb(first_sync_cap=500), 20) == 20


def test_cap_falls_back_to_the_global_value_when_unset():
    assert batch_cap(mb(first_sync_cap=None), 20) == 20


def test_no_cap_anywhere_is_zero_meaning_unbounded_by_the_caller():
    assert batch_cap(mb(first_sync_cap=None), None) == 0


def test_zero_global_value_does_not_override_a_real_cap():
    """fetch_per_run=0 means 'unbounded', not 'nothing' — the cap must still hold."""
    assert batch_cap(mb(first_sync_cap=500), 0) == 500


# ── alias lookup ─────────────────────────────────────────────────────────────

KNOWN = [
    KnownAddress("finance@projectwest.co.uk", "mb-fin", is_alias=False),
    KnownAddress("payments@projectwest.co.uk", "mb-fin", is_alias=True,
                 queue="payment_or_finance", reply_allowed=True),
    KnownAddress("safeguarding@projectwest.co.uk", "mb-coord", is_alias=True,
                 restricted=True),
]


def test_alias_lookup_finds_the_alias_row():
    a = alias_for(KNOWN, "payments@projectwest.co.uk")
    assert a is not None and a.reply_allowed is True


def test_alias_lookup_is_case_insensitive():
    assert alias_for(KNOWN, "PAYMENTS@ProjectWest.CO.UK") is not None


def test_mailbox_address_is_not_reported_as_an_alias():
    assert alias_for(KNOWN, "finance@projectwest.co.uk") is None


def test_unknown_or_missing_address_has_no_alias():
    assert alias_for(KNOWN, "nope@external.example") is None
    assert alias_for(KNOWN, None) is None


def test_restricted_alias_is_discoverable():
    a = alias_for(KNOWN, "safeguarding@projectwest.co.uk")
    assert a is not None and a.restricted is True
