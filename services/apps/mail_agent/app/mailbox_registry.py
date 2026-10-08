"""The mailbox and alias registry — system configuration, not user settings.

Two jobs:

  * build the known-address map that receiving_address.resolve() intersects against,
    combining the six physical mailbox addresses with every alias row, so the
    matching logic has one rule rather than two;
  * decide which mailboxes a sync run may touch at all.

The second is a safety gate, not an optimisation. `monitor_from` is NULL on a seeded
mailbox until Project West supplies an ingestion boundary, and a NULL boundary means
"walk the entire mailbox": info@ alone holds 161,833 items, so an accidental
unbounded first sync is a five-figure LLM bill rather than a slow afternoon. A
mailbox with no boundary is therefore skipped and the reason recorded, never
defaulted to "beginning of time".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Mailbox, MailboxAlias
from .receiving_address import KnownAddress

# Why a mailbox was skipped. Stable strings — they reach logs and the audit trail.
SKIP_NOT_PW = "not_a_pw_mailbox"
SKIP_DISABLED = "disabled"
SKIP_UNBOUNDED = "no_first_sync_bound"
SKIP_NO_GRAPH_ID = "no_graph_user_id"

# Retained for callers/log parsers written against the earlier rule, which required
# a monitor_from specifically. The bound can now come from either side.
SKIP_NO_MONITOR_FROM = SKIP_UNBOUNDED


@dataclass(frozen=True)
class SyncCandidate:
    """A mailbox considered for a sync run, with the verdict."""

    mailbox_id: str
    address: str
    graph_user_id: str | None
    monitor_from: datetime | None
    sync_cursor: datetime | None
    first_sync_cap: int | None
    eligible: bool
    skip_reason: str | None = None

    @property
    def since(self) -> datetime | None:
        """Where this run starts: the cursor once syncing, else the boundary.

        Never None for an eligible mailbox — that is what `eligible` guarantees.
        """
        return self.sync_cursor or self.monitor_from


def evaluate(
    *,
    mailbox_id: str,
    address: str | None,
    kind: str,
    enabled: bool,
    graph_user_id: str | None,
    monitor_from: datetime | None,
    sync_cursor: datetime | None = None,
    first_sync_cap: int | None = None,
) -> SyncCandidate:
    """Decide whether one mailbox may be synced. Pure — no database, no Graph."""
    reason: str | None = None
    if kind != "pw":
        reason = SKIP_NOT_PW
    elif not enabled:
        reason = SKIP_DISABLED
    elif not graph_user_id:
        reason = SKIP_NO_GRAPH_ID
    elif monitor_from is None and not first_sync_cap and sync_cursor is None:
        # A FIRST sync with neither a date floor nor a count ceiling is an
        # unbounded walk through the whole mailbox -- 85,120 items in info@, every
        # one an LLM call. Either bound is sufficient and they compose: the cap
        # limits how many, monitor_from limits how far back. Refusing here is what
        # makes "no boundary" fail loudly and cheaply instead of expensively.
        #
        # Only the first sync needs this. Once a cursor exists the mailbox is
        # walking forward from a known point and is inherently bounded.
        reason = SKIP_UNBOUNDED

    return SyncCandidate(
        mailbox_id=mailbox_id,
        address=address or "",
        graph_user_id=graph_user_id,
        monitor_from=monitor_from,
        sync_cursor=sync_cursor,
        first_sync_cap=first_sync_cap,
        eligible=reason is None,
        skip_reason=reason,
    )


def batch_cap(candidate: SyncCandidate, configured: int | None) -> int:
    """How many messages this run may take from this mailbox.

    The per-mailbox ceiling always wins over the configured per-run value. With six
    mailboxes sharing one global `fetch_per_run`, info@ -- 78.8% of the corpus --
    would otherwise consume the whole budget and starve referrals@, which is 0.08%
    of the corpus and the mailbox the business actually runs on.
    """
    caps = [c for c in (candidate.first_sync_cap, configured) if c]
    return min(caps) if caps else 0


async def load_known_addresses(session: AsyncSession) -> list[KnownAddress]:
    """The combined map of mailbox addresses and alias addresses.

    A mailbox's own address is itself an implicit alias of that mailbox, so both go
    into one map and the receiving-address matcher needs a single lookup rather than
    two code paths.
    """
    out: list[KnownAddress] = []

    mailboxes = (await session.execute(
        select(Mailbox).where(Mailbox.kind == "pw")
    )).scalars().all()
    by_id = {m.id: m for m in mailboxes}

    for m in mailboxes:
        if m.address:
            out.append(KnownAddress(
                address=m.address, mailbox_id=m.id, is_alias=False, business=True,
            ))

    aliases = (await session.execute(select(MailboxAlias))).scalars().all()
    for a in aliases:
        # Skip an alias whose parent is not a PW mailbox (e.g. pointing at a legacy
        # row): resolving to it would stamp an email with a mailbox nothing polls.
        if a.mailbox_id not in by_id:
            continue
        out.append(KnownAddress(
            address=a.address,
            mailbox_id=a.mailbox_id,
            is_alias=True,
            business=a.business,
            queue=a.queue,
            reply_allowed=a.reply_allowed,
            restricted=a.restricted,
        ))

    return out


async def load_sync_candidates(session: AsyncSession) -> list[SyncCandidate]:
    """Every mailbox with a verdict — including the skipped ones.

    Skips are returned rather than filtered out so the caller can report "info@ was
    not synced because it has no ingestion boundary" instead of silently doing
    nothing, which would be indistinguishable from "no new mail".
    """
    rows = (await session.execute(select(Mailbox))).scalars().all()
    return [
        evaluate(
            mailbox_id=m.id,
            address=m.address,
            kind=m.kind,
            enabled=m.enabled,
            graph_user_id=m.graph_user_id,
            monitor_from=m.monitor_from,
            sync_cursor=m.sync_cursor,
            first_sync_cap=m.first_sync_cap,
        )
        for m in rows
    ]


def alias_for(known: Sequence[KnownAddress], address: str | None) -> KnownAddress | None:
    """The alias row for a resolved receiving address, if it was an alias."""
    if not address:
        return None
    want = address.strip().lower()
    for k in known:
        if k.is_alias and k.address.strip().lower() == want:
            return k
    return None
