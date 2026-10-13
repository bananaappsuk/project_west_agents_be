"""The CRM's idempotency_key is length-capped; ours is derived, not truncated.

Every referral we sent was rejected 422 because a Graph message id is 152
characters and the CRM caps idempotency_key at 120. The obvious fix — clip to the
limit — is the dangerous one: a Graph id leads with the mailbox and folder and
carries the message's own bytes at the end, so two messages in one mailbox share a
long prefix. Clipped keys collide, and the CRM dedupes on that key, so the second
referral would come back as a replay of the first with no error raised at all.

These tests pin the properties that make the derived key safe.
"""

from __future__ import annotations

from app.crm_sync import _idempotency_key

#: Two real Graph message ids from the same mailbox. They are identical for the
#: first 100 characters and differ only near the end — which is exactly the case a
#: truncating implementation gets wrong.
UID_A = (
    "AAMkAGMxNTJjY2E1LTRiNjItNDU2ZS1iZGNkLWRmOTFhZGFiY2E0MABGAAAAAABFV55P2hLmTLnV"
    "rC1x9mVCBwAz6sXZ2c0HTphu93yX-lDOAAAAAAEMAAAz6sXZ2c0HTphu93yX-lDOAAA3NbB1AAA="
)
UID_B = (
    "AAMkAGMxNTJjY2E1LTRiNjItNDU2ZS1iZGNkLWRmOTFhZGFiY2E0MABGAAAAAABFV55P2hLmTLnV"
    "rC1x9mVCBwAz6sXZ2c0HTphu93yX-lDOAAAAAAEMAAAz6sXZ2c0HTphu93yX-lDOAAA3NbB2BBB="
)

#: The CRM's cap on this field today. Raised to 255 on their side, but the key
#: must stay inside the smaller number so a rollback on either side is survivable.
CRM_MAX = 120


def test_key_fits_the_crm_limit():
    assert len(_idempotency_key(UID_A)) == 64 <= CRM_MAX


def test_same_email_always_gives_the_same_key():
    """A retry has to dedupe against the original, not create a second referral."""
    assert _idempotency_key(UID_A) == _idempotency_key(UID_A)


def test_ids_sharing_a_long_prefix_get_different_keys():
    """The failure mode truncation would have introduced, pinned.

    These two ids agree for their first 100 characters. Truncated to the CRM's
    120-character limit they would still differ, but only by the tail — and any
    shorter clip would make them identical, so the CRM would treat the second
    referral as a replay of the first and return the first one's reference.
    """
    assert UID_A[:100] == UID_B[:100]
    assert _idempotency_key(UID_A) != _idempotency_key(UID_B)


def test_missing_uid_does_not_raise():
    """Capture must never be lost to a CRM detail; an empty id still yields a key."""
    assert len(_idempotency_key("")) == 64
    assert len(_idempotency_key(None)) == 64  # type: ignore[arg-type]
