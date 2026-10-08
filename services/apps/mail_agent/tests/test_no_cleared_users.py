"""Behaviour when NOBODY holds safeguarding clearance.

Project West decided (2026-10-07) not to name safeguarding leads for now, so
PW-Safeguarding-Lead is empty and stays empty. That makes "no cleared users" the
normal operating state rather than a temporary gap, and it needs to be a tested
state rather than an assumed one.

What must hold: restricted mail is still captured, stored, routed and audited in
full — it is only invisible. Nothing crashes, nothing is dropped, and the moment a
lead is named the backlog becomes visible with no reprocessing.
"""

from __future__ import annotations

from app.mailbox_scope import AccessRule, resolve
from app.receiving_address import KnownAddress
from app.receiving_address import resolve as resolve_address
from app.restriction import FLAG_SAFEGUARDING, ROUTE_SAFEGUARDING, apply_classification
from app.send_identity import (
    ALIAS_REPLY_FORBIDDEN,
    RESTRICTED_NO_AUTONOMOUS,
    OutboundContext,
    SendRefusal,
    derive,
)

G_COORD = "grp-coordinator"
SAFE_GROUP = "grp-safeguarding-lead"
MB_COORD = "mb-coordinator"

RULES = [AccessRule(G_COORD, MB_COORD, can_read=True, can_send=True, can_approve=True)]

KNOWN = [
    KnownAddress("coordinator@projectwest.co.uk", MB_COORD),
    KnownAddress("safeguarding@projectwest.co.uk", MB_COORD, is_alias=True,
                 queue=ROUTE_SAFEGUARDING, reply_allowed=False, restricted=True),
]


def scope_for(*groups):
    """Nobody is ever in SAFE_GROUP, and there are no clearance records."""
    return resolve(user_id="u1", group_ids=groups, rules=RULES,
                   safeguarding_group_id=SAFE_GROUP, has_clearance_record=False)


def to(*addrs):
    return [{"emailAddress": {"address": a}} for a in addrs]


# ── nobody has clearance ─────────────────────────────────────────────────────

def test_no_user_has_clearance_however_much_mailbox_access_they_hold():
    s = scope_for(G_COORD)
    assert s.readable == {MB_COORD}
    assert s.safeguarding is False
    assert s.visible_restricted() is False


# ── restricted mail is still fully captured ──────────────────────────────────

def test_safeguarding_mail_still_resolves_and_is_marked_restricted():
    """Invisible is not the same as unprocessed."""
    r = resolve_address(to_recipients=to("safeguarding@projectwest.co.uk"),
                        cc_recipients=None, headers=None, known=KNOWN)
    assert r.is_resolved
    assert r.restricted is True
    assert r.queue == ROUTE_SAFEGUARDING
    assert r.mailbox_id == MB_COORD


def test_classification_keeps_the_restriction_with_nobody_to_see_it():
    route, restricted, flags = apply_classification(
        current_route=ROUTE_SAFEGUARDING, current_restricted=True,
        alias_restricted=True, model_route="general_enquiry", model_flags=[])
    assert restricted is True
    assert route == ROUTE_SAFEGUARDING
    assert FLAG_SAFEGUARDING in flags


# ── and is never sent by anyone ──────────────────────────────────────────────

def test_the_agent_cannot_auto_reply_to_restricted_mail():
    got = derive(
        OutboundContext(mailbox_id=MB_COORD,
                        mailbox_address="coordinator@projectwest.co.uk",
                        receiving_address="coordinator@projectwest.co.uk",
                        receiving_address_status="resolved_single", restricted=True),
        scope=scope_for(G_COORD), autonomous=True)
    assert isinstance(got, SendRefusal)
    assert got.reason == RESTRICTED_NO_AUTONOMOUS


def test_nobody_can_reply_from_the_safeguarding_alias():
    """reply_allowed=false holds regardless of clearance — and nobody has it."""
    got = derive(
        OutboundContext(mailbox_id=MB_COORD,
                        mailbox_address="coordinator@projectwest.co.uk",
                        receiving_address="safeguarding@projectwest.co.uk",
                        receiving_address_status="resolved_single",
                        alias_address="safeguarding@projectwest.co.uk",
                        alias_reply_allowed=False, restricted=True),
        scope=scope_for(G_COORD), autonomous=False, actor_user_id="u1")
    assert isinstance(got, SendRefusal)
    assert got.reason == ALIAS_REPLY_FORBIDDEN


# ── the state is reversible without reprocessing ─────────────────────────────

def test_naming_one_lead_makes_the_whole_backlog_visible():
    """Clearance is evaluated at read time, so nothing needs re-ingesting or
    re-classifying when PW eventually names someone."""
    before = scope_for(G_COORD)
    after = resolve(user_id="u1", group_ids=[G_COORD, SAFE_GROUP], rules=RULES,
                    safeguarding_group_id=SAFE_GROUP, has_clearance_record=False)
    assert before.safeguarding is False
    assert after.safeguarding is True
    assert after.readable == before.readable, "clearance is not a mailbox grant"


def test_an_explicit_clearance_record_works_without_the_group():
    """A second route in, so PW is not forced through Entra to clear one person."""
    s = resolve(user_id="u1", group_ids=[G_COORD], rules=RULES,
                safeguarding_group_id=SAFE_GROUP, has_clearance_record=True)
    assert s.safeguarding is True
