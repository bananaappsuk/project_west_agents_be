"""Reply identity — SSOT acceptance coverage.

The From address is derived from the mailbox and the receiving alias, never supplied
by a caller, and the can_send check is the only thing standing between a user and
sending as a mailbox they have no rights to.
"""

from __future__ import annotations

import pytest

from app.mailbox_scope import MailboxScope
from app.send_identity import (
    ALIAS_REPLY_FORBIDDEN,
    NO_MAILBOX,
    NO_SEND_RIGHT,
    RESTRICTED_NO_AUTONOMOUS,
    UNRESOLVED_RECEIVING_ADDRESS,
    OutboundContext,
    SendIdentity,
    SendRefusal,
    derive,
)

MB_FIN, MB_COORD, MB_INFO = "mb-finance", "mb-coordinator", "mb-info"


def scope(sendable=(), readable=None, safeguarding=False):
    readable = frozenset(readable if readable is not None else sendable)
    return MailboxScope(
        user_id="u1",
        readable=readable,
        sendable=frozenset(sendable),
        approvable=frozenset(),
        safeguarding=safeguarding,
    )


def ctx(**kw):
    base = dict(
        mailbox_id=MB_FIN,
        mailbox_address="finance@projectwest.co.uk",
        receiving_address="finance@projectwest.co.uk",
        receiving_address_status="resolved_single",
    )
    base.update(kw)
    return OutboundContext(**base)


# ── the normal cases ─────────────────────────────────────────────────────────

def test_reply_from_the_mailbox_address():
    got = derive(ctx(), scope=scope([MB_FIN]), autonomous=False, actor_user_id="u1")
    assert isinstance(got, SendIdentity)
    assert got.from_address == "finance@projectwest.co.uk"
    assert got.alias_decision == "mailbox"
    assert got.approved_by == "u1"


def test_reply_to_alias_mail_goes_out_as_the_alias():
    """Mail that arrived at payments@ is replied to as payments@, not finance@."""
    got = derive(
        ctx(receiving_address="payments@projectwest.co.uk",
            alias_address="payments@projectwest.co.uk",
            alias_reply_allowed=True),
        scope=scope([MB_FIN]), autonomous=False, actor_user_id="u1")
    assert isinstance(got, SendIdentity)
    assert got.from_address == "payments@projectwest.co.uk"
    assert got.alias_decision == "alias:payments@projectwest.co.uk"


def test_alias_configured_but_message_arrived_at_the_mailbox_replies_as_mailbox():
    """finance@ has a payments@ alias, but this message came to finance@ directly."""
    got = derive(
        ctx(receiving_address="finance@projectwest.co.uk",
            alias_address="payments@projectwest.co.uk",
            alias_reply_allowed=True),
        scope=scope([MB_FIN]), autonomous=False)
    assert isinstance(got, SendIdentity)
    assert got.from_address == "finance@projectwest.co.uk"
    assert got.alias_decision == "mailbox"


def test_autonomous_send_is_allowed_for_ordinary_resolved_mail():
    got = derive(ctx(), scope=scope([MB_FIN]), autonomous=True)
    assert isinstance(got, SendIdentity)


# ── can_send is the only control ─────────────────────────────────────────────

def test_user_without_can_send_is_refused():
    got = derive(ctx(), scope=scope(sendable=(), readable=[MB_FIN]), autonomous=False)
    assert isinstance(got, SendRefusal)
    assert got.reason == NO_SEND_RIGHT


def test_send_right_on_another_mailbox_does_not_transfer():
    """can_send on info@ must not permit sending as finance@."""
    got = derive(ctx(), scope=scope([MB_INFO]), autonomous=False)
    assert isinstance(got, SendRefusal)
    assert got.reason == NO_SEND_RIGHT


def test_send_right_is_checked_before_alias_rules_leak_configuration():
    """A user with no rights learns nothing about alias configuration."""
    got = derive(
        ctx(receiving_address="payments@projectwest.co.uk",
            alias_address="payments@projectwest.co.uk",
            alias_reply_allowed=False),
        scope=scope(sendable=(), readable=[MB_FIN]), autonomous=False)
    assert got.reason == NO_SEND_RIGHT, "not ALIAS_REPLY_FORBIDDEN"


# ── safeguarding ─────────────────────────────────────────────────────────────

def test_safeguarding_alias_cannot_be_replied_from_even_by_a_rights_holder():
    """reply_allowed=false: no user and no agent sends from safeguarding@."""
    got = derive(
        ctx(mailbox_id=MB_COORD, mailbox_address="coordinator@projectwest.co.uk",
            receiving_address="safeguarding@projectwest.co.uk",
            alias_address="safeguarding@projectwest.co.uk",
            alias_reply_allowed=False, restricted=True),
        scope=scope([MB_COORD], safeguarding=True), autonomous=False)
    assert isinstance(got, SendRefusal)
    assert got.reason == ALIAS_REPLY_FORBIDDEN


def test_restricted_item_is_never_auto_replied():
    got = derive(ctx(restricted=True), scope=scope([MB_FIN]), autonomous=True)
    assert isinstance(got, SendRefusal)
    assert got.reason == RESTRICTED_NO_AUTONOMOUS


def test_restricted_item_arriving_at_a_normal_address_still_blocks_autonomy():
    """info@ mail the classifier flagged safeguarding_indicator is still restricted."""
    got = derive(
        ctx(mailbox_id=MB_INFO, mailbox_address="info@projectwest.co.uk",
            receiving_address="info@projectwest.co.uk", restricted=True),
        scope=scope([MB_INFO]), autonomous=True)
    assert isinstance(got, SendRefusal)
    assert got.reason == RESTRICTED_NO_AUTONOMOUS


def test_human_may_reply_to_a_restricted_item_from_a_replyable_address():
    """The bar is on autonomy, not on cleared humans."""
    got = derive(
        ctx(mailbox_id=MB_INFO, mailbox_address="info@projectwest.co.uk",
            receiving_address="info@projectwest.co.uk", restricted=True),
        scope=scope([MB_INFO], safeguarding=True), autonomous=False,
        actor_user_id="lead1")
    assert isinstance(got, SendIdentity)
    assert got.approved_by == "lead1"


def test_restriction_is_checked_before_send_rights():
    """An autonomous restricted send is refused as restricted, not as a rights gap."""
    got = derive(ctx(restricted=True), scope=scope(sendable=(), readable=[MB_FIN]),
                 autonomous=True)
    assert got.reason == RESTRICTED_NO_AUTONOMOUS


# ── exception states never produce an identity ───────────────────────────────

@pytest.mark.parametrize("status", ["unresolved", "ambiguous", "legacy"])
def test_non_resolved_receiving_address_refuses(status):
    got = derive(ctx(receiving_address=None, receiving_address_status=status),
                 scope=scope([MB_FIN]), autonomous=False)
    assert isinstance(got, SendRefusal)
    assert got.reason == UNRESOLVED_RECEIVING_ADDRESS


def test_resolved_by_precedence_is_sendable():
    got = derive(ctx(receiving_address_status="resolved_precedence"),
                 scope=scope([MB_FIN]), autonomous=False)
    assert isinstance(got, SendIdentity)


def test_email_with_no_mailbox_identity_refuses():
    got = derive(ctx(mailbox_id=None, mailbox_address=None),
                 scope=scope([MB_FIN]), autonomous=False)
    assert isinstance(got, SendRefusal)
    assert got.reason == NO_MAILBOX


# ── the identity cannot be influenced from outside ───────────────────────────

def test_outbound_context_has_no_caller_supplied_sender_field():
    """Structural guard: if someone adds a `from` here, this test fails loudly."""
    fields = set(OutboundContext.__dataclass_fields__)
    for forbidden in ("from_address", "sender", "from_", "reply_from"):
        assert forbidden not in fields, f"{forbidden} must not be caller-supplied"


def test_identity_is_frozen():
    got = derive(ctx(), scope=scope([MB_FIN]), autonomous=False)
    with pytest.raises(Exception):
        got.from_address = "attacker@evil.example"  # type: ignore[misc]


def test_refusal_is_never_truthy_as_allowed():
    got = derive(ctx(), scope=scope(sendable=()), autonomous=False)
    assert isinstance(got, SendRefusal)
    assert got.allowed is False
