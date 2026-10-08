"""Graph request shape — no tenant, no network.

Covers the two things the POC was missing: the $select fields that make the
receiving address recoverable, and the `from` property on sendMail.
"""

from __future__ import annotations

from app.graph_client import _SELECT_FIELDS, build_send_payload


# ── $select: receiving-address evidence must be requested ────────────────────

def test_select_requests_recipient_fields():
    for field in ("toRecipients", "ccRecipients", "internetMessageHeaders"):
        assert field in _SELECT_FIELDS, f"{field} is required to recover the alias"


def test_select_requests_identity_fields():
    for field in ("conversationId", "internetMessageId"):
        assert field in _SELECT_FIELDS


def test_select_still_requests_the_original_fields():
    """Extending the projection must not drop what the pipeline already used."""
    for field in ("id", "subject", "from", "receivedDateTime", "body", "hasAttachments"):
        assert field in _SELECT_FIELDS


# ── sendMail payload ─────────────────────────────────────────────────────────

def test_payload_has_no_from_when_none_derived():
    """Omitted rather than defaulted: Graph then uses the mailbox's own address."""
    p = build_send_payload(to_addr="x@external.example", subject="S", body="B")
    assert "from" not in p["message"]
    assert p["saveToSentItems"] == "true"


def test_payload_sets_from_when_an_alias_was_derived():
    p = build_send_payload(to_addr="x@external.example", subject="S", body="B",
                           from_address="payments@projectwest.co.uk")
    assert p["message"]["from"] == {
        "emailAddress": {"address": "payments@projectwest.co.uk"}}


def test_payload_preserves_recipient_subject_and_body():
    p = build_send_payload(to_addr="x@external.example", subject="Re: hello",
                           body="body text")
    assert p["message"]["toRecipients"] == [
        {"emailAddress": {"address": "x@external.example"}}]
    assert p["message"]["subject"] == "Re: hello"
    assert p["message"]["body"]["content"] == "body text"


def test_empty_from_address_is_treated_as_absent():
    """An empty string must not produce `from: {address: ""}`, which Graph rejects."""
    p = build_send_payload(to_addr="x@external.example", subject="S", body="B",
                           from_address="")
    assert "from" not in p["message"]


def test_sent_items_copy_is_always_requested():
    """The reply must land in the shared mailbox's Sent Items."""
    p = build_send_payload(to_addr="x@external.example", subject="S", body="B",
                           from_address="info@projectwest.co.uk")
    assert p["saveToSentItems"] == "true"
