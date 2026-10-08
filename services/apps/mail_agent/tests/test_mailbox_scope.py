"""Per-mailbox authorization — SSOT acceptance coverage.

This is the only control standing between a user and a mailbox they should not see:
app-only Graph access bypasses Exchange's own Full Access / Send As checks entirely.
These tests are therefore written adversarially — most of them are negative.
"""

from __future__ import annotations

import pytest

from app.mailbox_scope import (
    AccessRule,
    EmptyScopeError,
    MailboxForbidden,
    MailboxScope,
    groups_from_claims,
    resolve,
)

G_INFO_READ = "grp-info-read"
G_INFO_SEND = "grp-info-send"
G_FIN_RW = "grp-finance-rw"
G_MGMT_RW = "grp-management-rw"
G_SAFE = "grp-safeguarding-lead"

MB_INFO, MB_FIN, MB_MGMT = "mb-info", "mb-finance", "mb-management"

RULES = [
    AccessRule(G_INFO_READ, MB_INFO, can_read=True),
    AccessRule(G_INFO_SEND, MB_INFO, can_read=True, can_send=True),
    AccessRule(G_FIN_RW, MB_FIN, can_read=True, can_send=True, can_approve=True),
    AccessRule(G_MGMT_RW, MB_MGMT, can_read=True, can_send=True),
]


def scope(*groups, clearance=False):
    return resolve(user_id="u1", group_ids=groups, rules=RULES,
                   safeguarding_group_id=G_SAFE, has_clearance_record=clearance)


# ── resolution ───────────────────────────────────────────────────────────────

def test_no_groups_yields_empty_scope():
    s = scope()
    assert s.is_empty
    assert s.readable == frozenset()


def test_union_across_overlapping_groups():
    """Two groups granting the same mailbox produce the union, not last-wins."""
    s = scope(G_INFO_READ, G_INFO_SEND)
    assert s.readable == {MB_INFO}
    assert s.sendable == {MB_INFO}, "send from the second group must survive"


def test_read_only_group_grants_no_send():
    s = scope(G_INFO_READ)
    assert s.readable == {MB_INFO}
    assert s.sendable == frozenset()
    assert s.approvable == frozenset()


def test_unrelated_group_grants_nothing():
    s = scope("grp-some-other-team")
    assert s.is_empty


def test_multiple_mailboxes_accumulate():
    s = scope(G_INFO_READ, G_FIN_RW)
    assert s.readable == {MB_INFO, MB_FIN}
    assert s.approvable == {MB_FIN}


def test_send_without_read_is_stripped():
    """A misconfigured rule must not create send-but-not-read."""
    s = resolve(user_id="u1", group_ids=["g"],
                rules=[AccessRule("g", MB_FIN, can_read=False, can_send=True,
                                  can_approve=True)])
    assert s.readable == frozenset()
    assert s.sendable == frozenset(), "send cannot exceed read"
    assert s.approvable == frozenset()


# ── assertions: 404, never 403 ───────────────────────────────────────────────

def test_out_of_scope_read_raises_404_not_403():
    s = scope(G_INFO_READ)
    with pytest.raises(MailboxForbidden) as e:
        s.assert_readable(MB_MGMT)
    assert e.value.status_code == 404, "403 would confirm the mailbox exists"


def test_in_scope_read_passes():
    scope(G_INFO_READ).assert_readable(MB_INFO)


def test_send_requires_can_send():
    s = scope(G_INFO_READ)
    s.assert_readable(MB_INFO)
    with pytest.raises(MailboxForbidden):
        s.assert_sendable(MB_INFO)


def test_approve_requires_can_approve():
    s = scope(G_MGMT_RW)
    s.assert_sendable(MB_MGMT)
    with pytest.raises(MailboxForbidden):
        s.assert_approvable(MB_MGMT)


# ── narrowing: a request can never widen ─────────────────────────────────────

def test_request_without_filter_returns_full_authorised_set():
    assert scope(G_INFO_READ, G_FIN_RW).narrow(None) == {MB_INFO, MB_FIN}


def test_requested_mailbox_narrows():
    assert scope(G_INFO_READ, G_FIN_RW).narrow([MB_FIN]) == {MB_FIN}


def test_requested_out_of_scope_mailbox_cannot_widen():
    """The crafted-parameter attack: asking for Management must yield nothing."""
    assert scope(G_INFO_READ).narrow([MB_MGMT]) == frozenset()


def test_mixed_request_keeps_only_authorised_ids():
    assert scope(G_INFO_READ).narrow([MB_INFO, MB_MGMT, MB_FIN]) == {MB_INFO}


def test_empty_request_list_yields_nothing_not_everything():
    """An explicit empty filter must not fall through to 'all mailboxes'."""
    assert scope(G_INFO_READ, G_FIN_RW).narrow([]) == frozenset()


# ── safeguarding: a separate axis ────────────────────────────────────────────

def test_mailbox_access_alone_does_not_grant_clearance():
    """Every named PW user can read coordinator@, which safeguarding@ routes into."""
    s = scope(G_INFO_READ, G_FIN_RW, G_MGMT_RW)
    assert s.safeguarding is False
    assert s.visible_restricted() is False


def test_safeguarding_group_grants_clearance():
    assert scope(G_INFO_READ, G_SAFE).safeguarding is True


def test_explicit_clearance_record_grants_clearance():
    assert scope(G_INFO_READ, clearance=True).safeguarding is True


def test_clearance_without_any_mailbox_access_reads_nothing():
    """Clearance is not a mailbox grant — it only unhides restricted rows."""
    s = scope(G_SAFE)
    assert s.safeguarding is True
    assert s.is_empty


def test_superadmin_style_broad_access_does_not_imply_clearance():
    """The role that configures the system is not the one that handles disclosures."""
    s = resolve(user_id="admin", group_ids=[G_INFO_READ, G_FIN_RW, G_MGMT_RW],
                rules=RULES, safeguarding_group_id=G_SAFE)
    assert s.readable == {MB_INFO, MB_FIN, MB_MGMT}
    assert s.safeguarding is False


# ── group claims and the overage case ────────────────────────────────────────

def test_groups_read_from_claim():
    assert groups_from_claims({"groups": ["a", "b"]}) == ["a", "b"]


def test_overage_without_fallback_fails_loudly():
    """Must not resolve to an empty set — that would silently revoke everyone."""
    with pytest.raises(EmptyScopeError):
        groups_from_claims({"_claim_names": {"groups": "src1"}})


def test_overage_uses_memberof_fallback():
    got = groups_from_claims({"_claim_names": {"groups": "src1"}},
                             memberof_fallback=["a", "b"])
    assert got == ["a", "b"]


def test_absent_groups_claim_without_fallback_fails_loudly():
    with pytest.raises(EmptyScopeError):
        groups_from_claims({})


# ── immutability ─────────────────────────────────────────────────────────────

def test_scope_is_frozen():
    s = scope(G_INFO_READ)
    with pytest.raises(Exception):
        s.readable = frozenset({MB_MGMT})  # type: ignore[misc]


def test_narrow_does_not_mutate_the_scope():
    s = scope(G_INFO_READ, G_FIN_RW)
    s.narrow([MB_FIN])
    assert s.readable == {MB_INFO, MB_FIN}
