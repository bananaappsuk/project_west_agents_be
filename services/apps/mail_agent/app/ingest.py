"""Project West multi-mailbox ingestion (capture phase).

Fetches from the six approved shared mailboxes via Graph app-only, resolves the
business receiving address, and persists each message with its mailbox identity and
the evidence the decision was made from.

Deliberately separate from pipeline.py's existing single-mailbox path rather than
retrofitted into it. Two reasons: the old path is built around the
`ux_mailboxes_singleton` assumption that no longer holds, and the SSOT's own
sequencing caution says not to remove the IMAP route until Graph is proven across all
six mailboxes. Keeping them side by side means the fallback still exists while this
is being verified.

This module captures only. Classification, routing, CRM sync and drafting stay where
they are — capture goes first because the receiving address is the one piece of
evidence that cannot be recovered after collection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import graph_client, mailbox_registry, receiving_address
from .models import Email, EmailAudit, Mailbox
from .receiving_address import KnownAddress

log = logging.getLogger("mail_agent.ingest")

#: Flag written onto a row whose business entry point could not be determined, so the
#: exception set is queryable without reasoning about the status enum everywhere.
FLAG_RECEIVING_UNRESOLVED = "receiving_address_unresolved"
#: Taxonomy route for an item with no safe classification. Matches the value the
#: taxonomy migration already uses, so the exception queue is one concept.
ROUTE_EXCEPTION = "exception"


@dataclass
class IngestResult:
    mailbox_address: str
    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    unresolved: int = 0
    restricted: int = 0
    skipped_reason: str | None = None
    cursor_advanced_to: datetime | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.skipped_reason is None and not self.errors


def prepare_row(
    msg: Mapping,
    *,
    mailbox_id: str,
    mailbox_address: str,
    known: Sequence[KnownAddress],
    precedence: Sequence[str] | None = None,
) -> dict:
    """Build the persistable column values for one Graph message. Pure.

    The receiving-address decision and its evidence are computed here so they can be
    asserted without a database or a tenant.
    """
    res = receiving_address.resolve(
        to_recipients=msg.get("toRecipients"),
        cc_recipients=msg.get("ccRecipients"),
        headers=msg.get("internetMessageHeaders"),
        known=known,
        precedence=precedence,
    )

    alias = mailbox_registry.alias_for(known, res.receiving_address)

    row: dict = {
        "uid": msg.get("uid"),
        "sender": msg.get("from") or "",
        "from_email": msg.get("fromEmail") or "",
        "subject": msg.get("subject") or "",
        "body": msg.get("body") or "",
        "content_type": msg.get("contentType") or "text",
        "received_at": msg.get("receivedAt"),
        "conversation_id": msg.get("conversationId"),
        "internet_message_id": msg.get("internetMessageId"),
        # The authorization key. Always the mailbox actually polled — never derived
        # from the message, which is why an ambiguous receiving address cannot move a
        # row into a mailbox the reader is not entitled to.
        "mailbox_id": mailbox_id,
        "receiving_address_status": res.status,
        "recipient_evidence": dict(res.evidence),
        "restricted": res.restricted,
    }

    if res.is_resolved:
        row["receiving_address"] = res.receiving_address
        row["original_recipient"] = res.original_recipient
    else:
        # NOT silently defaulted to the polled mailbox as a business fact. The column
        # is NOT NULL, so it carries the polled address as a storage placeholder --
        # and `receiving_address_status` is what says it is not a determination.
        # Nothing may read one without the other.
        row["receiving_address"] = mailbox_address
        row["original_recipient"] = None
        row["flags"] = [FLAG_RECEIVING_UNRESOLVED]
        row["route"] = ROUTE_EXCEPTION

    # An alias may carry a queue the classifier should not override, and a
    # restriction that wins regardless of what the model later concludes.
    if alias and alias.queue:
        row["route"] = alias.queue

    return row


async def _known_uids(session: AsyncSession, mailbox_id: str) -> set[str]:
    """UIDs already stored for this mailbox.

    Scoped per mailbox on purpose: the same message legitimately appears in two
    monitored mailboxes (sent to both info@ and referrals@), and under Graph each
    copy has its own message id. A global UID check would be harmless here but a
    global internetMessageId check would wrongly drop the second copy.
    """
    rows = (await session.execute(
        select(Email.uid).where(Email.mailbox_id == mailbox_id)
    )).scalars().all()
    return {r for r in rows if r}


async def ingest_mailbox(
    session: AsyncSession,
    candidate: mailbox_registry.SyncCandidate,
    known: Sequence[KnownAddress],
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    cap: int,
    precedence: Sequence[str] | None = None,
) -> IngestResult:
    """Capture new mail from one mailbox. Commits nothing — the caller does."""
    out = IngestResult(mailbox_address=candidate.address)

    if not candidate.eligible:
        out.skipped_reason = candidate.skip_reason
        log.info("ingest skipped mailbox=%s reason=%s",
                 candidate.address, candidate.skip_reason)
        return out

    seen = await _known_uids(session, candidate.mailbox_id)

    # First sync of this mailbox: take the newest `cap` and stop. Project West's
    # rule is "seed with the latest few, never backfill", so there is no window to
    # walk and no history to catch up on. Every later sync walks forward from the
    # cursor instead.
    first_sync = candidate.sync_cursor is None

    try:
        fetched = graph_client.fetch_latest(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret,
            mailbox=candidate.address, count=cap, known_uids=seen,
            # monitor_from stays a hard floor even on a first sync: it is the one
            # thing that guarantees nothing ever reaches further back than PW
            # intended, whichever ordering is in play.
            since_at=candidate.since,
            newest_first=first_sync,
        )
    except Exception as exc:  # noqa: BLE001 - one mailbox must not sink the run
        out.errors.append(f"{type(exc).__name__}: {exc}")
        log.warning("ingest FAILED mailbox=%s: %s", candidate.address, exc)
        return out

    messages, watermark = fetched.messages, fetched.max_received_at
    out.fetched = len(messages)

    for msg in messages:
        # Second dedup layer, and expected to stay at zero: graph_client.fetch_latest
        # already filters `known_uids` before returning, so a hit here means the
        # primary filter was bypassed or the watermark and the stored rows disagree.
        # Kept because the cost is one set lookup and the alternative is a duplicate
        # row, but `duplicates` reading 0 is the healthy case, not a sign dedup is
        # untested — the real guarantee is the unique (uid_validity, uid) constraint.
        if msg.get("uid") in seen:
            out.duplicates += 1
            continue
        row = prepare_row(
            msg, mailbox_id=candidate.mailbox_id,
            mailbox_address=candidate.address, known=known, precedence=precedence,
        )
        email = Email(**row)
        session.add(email)
        await session.flush()  # assigns email.id for the audit row below

        if row["receiving_address_status"] not in receiving_address.RESOLVED_STATUSES:
            out.unresolved += 1
        if row["restricted"]:
            out.restricted += 1

        # Run-level counters are not a substitute for this: the audit record has to
        # say what happened to a specific message, not how many were handled.
        session.add(EmailAudit(
            email_id=email.id,
            mailbox_id=candidate.mailbox_id,
            action="ingested",
            actor_kind="system",
            detail={
                "receiving_address_status": row["receiving_address_status"],
                "receiving_address": row.get("receiving_address"),
                "original_recipient": row.get("original_recipient"),
                "restricted": row["restricted"],
                "route": row.get("route"),
                "candidates": row["recipient_evidence"].get("candidates", []),
            },
        ))
        seen.add(row["uid"])
        out.inserted += 1

    # Advance the per-mailbox watermark only as far as this batch actually scanned.
    if watermark:
        mbx = await session.get(Mailbox, candidate.mailbox_id)
        if mbx is not None:
            current = mbx.sync_cursor
            if current is None or watermark > current:
                mbx.sync_cursor = watermark
                out.cursor_advanced_to = watermark
        mbx_ts = datetime.now(timezone.utc)
        if mbx is not None:
            mbx.last_synced_at = mbx_ts

    log.info("ingest mailbox=%s fetched=%d inserted=%d dup=%d unresolved=%d restricted=%d",
             candidate.address, out.fetched, out.inserted, out.duplicates,
             out.unresolved, out.restricted)
    return out


async def ingest_all(
    session: AsyncSession,
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    configured_cap: int,
    precedence: Sequence[str] | None = None,
) -> list[IngestResult]:
    """Capture from every eligible mailbox, in ascending backlog order.

    Smallest first is deliberate. With a shared per-run budget info@ (85,120 inbound)
    would otherwise consume it and starve referrals@ (63) -- the mailbox the business
    actually runs on. The per-mailbox cap in mailbox_registry.batch_cap is the real
    guarantee; ordering just means the cheap mailboxes are never the ones waiting.
    """
    candidates = await mailbox_registry.load_sync_candidates(session)
    known = await mailbox_registry.load_known_addresses(session)

    results: list[IngestResult] = []
    for c in candidates:
        results.append(await ingest_mailbox(
            session, c, known,
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret,
            cap=mailbox_registry.batch_cap(c, configured_cap),
            precedence=precedence,
        ))
    return results
