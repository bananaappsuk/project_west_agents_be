"""Row preparation at capture time — SSOT acceptance coverage.

prepare_row() is where the receiving-address decision becomes a persisted fact. It
is pure, so every branch is tested without a database or a tenant.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.ingest import FLAG_RECEIVING_UNRESOLVED, ROUTE_EXCEPTION, prepare_row
from app.receiving_address import KnownAddress

MB_FIN, MB_COORD = "mb-finance", "mb-coordinator"

KNOWN = [
    KnownAddress("finance@projectwest.co.uk", MB_FIN),
    KnownAddress("coordinator@projectwest.co.uk", MB_COORD),
    KnownAddress("payments@projectwest.co.uk", MB_FIN, is_alias=True,
                 queue="payment_or_finance", reply_allowed=True),
    KnownAddress("safeguarding@projectwest.co.uk", MB_COORD, is_alias=True,
                 queue="safeguarding_restricted", restricted=True),
    KnownAddress("complaints@projectwest.co.uk", MB_COORD, is_alias=True,
                 queue="complaint", reply_allowed=True),
]


def msg(**kw):
    base = dict(
        uid="AAMk-1",
        **{"from": "A Sender"},
        fromEmail="sender@external.example",
        subject="Hello",
        body="body",
        contentType="text",
        receivedAt=datetime(2026, 10, 7, tzinfo=timezone.utc),
        conversationId="conv-1",
        internetMessageId="<abc@external.example>",
        toRecipients=[],
        ccRecipients=[],
        internetMessageHeaders=[],
    )
    base.update(kw)
    return base


def to(*addrs):
    return [{"emailAddress": {"address": a}} for a in addrs]


def row_for(**kw):
    return prepare_row(msg(**kw), mailbox_id=MB_FIN,
                       mailbox_address="finance@projectwest.co.uk", known=KNOWN)


# ── identity and passthrough ─────────────────────────────────────────────────

def test_mailbox_id_is_the_polled_mailbox_always():
    r = row_for(toRecipients=to("finance@projectwest.co.uk"))
    assert r["mailbox_id"] == MB_FIN


def test_identity_fields_are_captured():
    r = row_for(toRecipients=to("finance@projectwest.co.uk"))
    assert r["conversation_id"] == "conv-1"
    assert r["internet_message_id"] == "<abc@external.example>"


def test_core_message_fields_are_preserved():
    r = row_for(toRecipients=to("finance@projectwest.co.uk"))
    assert r["uid"] == "AAMk-1"
    assert r["from_email"] == "sender@external.example"
    assert r["subject"] == "Hello"
    assert r["content_type"] == "text"


# ── resolved cases ───────────────────────────────────────────────────────────

def test_mailbox_address_resolves_cleanly():
    r = row_for(toRecipients=to("finance@projectwest.co.uk"))
    assert r["receiving_address_status"] == "resolved_single"
    assert r["receiving_address"] == "finance@projectwest.co.uk"
    assert r["original_recipient"] is None
    assert "flags" not in r and "route" not in r


def test_alias_sets_original_recipient_and_queue():
    r = row_for(toRecipients=to("payments@projectwest.co.uk"))
    assert r["receiving_address"] == "payments@projectwest.co.uk"
    assert r["original_recipient"] == "payments@projectwest.co.uk"
    assert r["route"] == "payment_or_finance"
    assert r["restricted"] is False


def test_safeguarding_alias_sets_restriction_and_route():
    r = prepare_row(msg(toRecipients=to("safeguarding@projectwest.co.uk")),
                    mailbox_id=MB_COORD,
                    mailbox_address="coordinator@projectwest.co.uk", known=KNOWN)
    assert r["restricted"] is True
    assert r["route"] == "safeguarding_restricted"


def test_complaints_and_safeguarding_are_distinguishable_in_the_same_mailbox():
    """Both land in coordinator@; only the receiving address tells them apart."""
    a = prepare_row(msg(uid="a", toRecipients=to("complaints@projectwest.co.uk")),
                    mailbox_id=MB_COORD,
                    mailbox_address="coordinator@projectwest.co.uk", known=KNOWN)
    b = prepare_row(msg(uid="b", toRecipients=to("safeguarding@projectwest.co.uk")),
                    mailbox_id=MB_COORD,
                    mailbox_address="coordinator@projectwest.co.uk", known=KNOWN)
    assert a["mailbox_id"] == b["mailbox_id"]
    assert a["route"] != b["route"]
    assert a["restricted"] is False and b["restricted"] is True


# ── exception cases: marked, never silently defaulted ───────────────────────

def test_unresolved_is_flagged_and_routed_to_exception():
    r = row_for(toRecipients=to("someone@external.example"))
    assert r["receiving_address_status"] == "unresolved"
    assert r["flags"] == [FLAG_RECEIVING_UNRESOLVED]
    assert r["route"] == ROUTE_EXCEPTION
    assert r["original_recipient"] is None


def test_unresolved_placeholder_is_the_polled_mailbox_but_status_says_otherwise():
    """The NOT NULL column gets a value; the status is what makes it not a fact."""
    r = row_for(toRecipients=to("someone@external.example"))
    assert r["receiving_address"] == "finance@projectwest.co.uk"
    assert r["receiving_address_status"] != "resolved_single"


def test_ambiguous_is_flagged_not_resolved():
    r = row_for(toRecipients=to("finance@projectwest.co.uk",
                                "coordinator@projectwest.co.uk"))
    assert r["receiving_address_status"] == "ambiguous"
    assert r["route"] == ROUTE_EXCEPTION


def test_safeguarding_alias_beats_its_own_parent_mailbox():
    """Observed live: a message naming both safeguarding@ and coordinator@ is one
    destination described twice, so the alias wins and the row is routed restricted
    rather than dumped in the exception queue."""
    r = prepare_row(msg(toRecipients=to("safeguarding@projectwest.co.uk",
                                        "coordinator@projectwest.co.uk")),
                    mailbox_id=MB_COORD,
                    mailbox_address="coordinator@projectwest.co.uk", known=KNOWN)
    assert r["receiving_address_status"] == "resolved_precedence"
    assert r["receiving_address"] == "safeguarding@projectwest.co.uk"
    assert r["restricted"] is True
    assert r["route"] == "safeguarding_restricted"


def test_genuinely_ambiguous_row_touching_safeguarding_still_restricts():
    """Two different mailboxes — stays ambiguous, but restriction survives."""
    r = prepare_row(msg(toRecipients=to("safeguarding@projectwest.co.uk",
                                        "finance@projectwest.co.uk")),
                    mailbox_id=MB_COORD,
                    mailbox_address="coordinator@projectwest.co.uk", known=KNOWN)
    assert r["receiving_address_status"] == "ambiguous"
    assert r["restricted"] is True, "restriction must survive ambiguity"
    assert r["route"] == ROUTE_EXCEPTION


# ── evidence ─────────────────────────────────────────────────────────────────

def test_evidence_is_always_persisted():
    r = row_for(toRecipients=to("x@external.example"),
                ccRecipients=to("y@external.example"))
    ev = r["recipient_evidence"]
    assert ev["to"] == ["x@external.example"]
    assert ev["cc"] == ["y@external.example"]
    assert ev["candidates"] == []


def test_evidence_is_a_plain_dict_for_jsonb():
    r = row_for(toRecipients=to("finance@projectwest.co.uk"))
    assert isinstance(r["recipient_evidence"], dict)


def test_header_recovered_alias_is_resolved_and_evidenced():
    r = row_for(toRecipients=to("undisclosed@example.com"),
                internetMessageHeaders=[
                    {"name": "Delivered-To", "value": "payments@projectwest.co.uk"}])
    assert r["receiving_address_status"] == "resolved_single"
    assert r["original_recipient"] == "payments@projectwest.co.uk"
    assert r["recipient_evidence"]["delivery_headers"] == ["payments@projectwest.co.uk"]


# ── precedence ───────────────────────────────────────────────────────────────

def test_precedence_resolves_and_applies_the_winning_alias_queue():
    r = prepare_row(
        msg(toRecipients=to("coordinator@projectwest.co.uk",
                            "complaints@projectwest.co.uk")),
        mailbox_id=MB_COORD, mailbox_address="coordinator@projectwest.co.uk",
        known=KNOWN, precedence=["complaints@projectwest.co.uk"])
    assert r["receiving_address_status"] == "resolved_precedence"
    assert r["route"] == "complaint"
