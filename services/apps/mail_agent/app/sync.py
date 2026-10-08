"""One scheduled run: capture from every approved mailbox, then analyse.

This is what the cron tick calls. It replaces the single-mailbox
`pipeline.fetch_and_process` path for Project West: that function selects one
mailbox with a bare `scalar()`, which was correct while `ux_mailboxes_singleton`
guaranteed exactly one row and is wrong now that there are six.

Capture and analysis are deliberately separate phases. Capture is cheap, must not be
skipped, and loses evidence that cannot be recovered if it does not happen;
analysis is expensive and can be retried at leisure against rows already safely
stored. Ordering them this way means an LLM outage costs a delay rather than a
permanent loss of the receiving address.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from . import ingest, pipeline
from .config import settings
from .db import SessionLocal
from .models import AgentConfig, AgentRun, Email

log = logging.getLogger("mail_agent.sync")


@dataclass
class SyncReport:
    captured: int = 0
    analysed: int = 0
    high_priority: int = 0
    mailboxes_synced: int = 0
    mailboxes_skipped: int = 0
    unresolved: int = 0
    restricted: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


async def run_once(*, analyse: bool = True, analyse_limit: int = 50) -> SyncReport:
    """Capture from all eligible mailboxes, then analyse what is outstanding.

    Records an AgentRun row for the attempt. That row is not bookkeeping: the
    scheduler decides whether a run is due from `max(AgentRun.run_at)`, so a run
    that leaves no trace makes every subsequent tick look like the first one and
    the configured interval is silently ignored. It is also what the UI reads for
    "last synced" — without it the app reports "never synced" while syncing
    perfectly well every few minutes.
    """
    report = SyncReport()

    # Written up-front with status="running" so a run that crashes outright still
    # leaves evidence it was attempted, rather than vanishing.
    async with SessionLocal() as session:
        run = AgentRun(status="running")
        session.add(run)
        await session.commit()
        run_id = run.id

    if not settings.graph_configured:
        report.errors.append("graph not configured (GRAPH_TENANT_ID/CLIENT_ID/CLIENT_SECRET)")
        log.error("sync aborted: Graph credentials missing")
        await _finish(run_id, report)
        return report

    # ── capture ──────────────────────────────────────────────────────────────
    async with SessionLocal() as session:
        results = await ingest.ingest_all(
            session,
            tenant_id=settings.graph_tenant_id,     # type: ignore[arg-type]
            client_id=settings.graph_client_id,     # type: ignore[arg-type]
            client_secret=settings.graph_client_secret,  # type: ignore[arg-type]
            configured_cap=settings.fetch_per_run,
        )
        # Committed once, after every mailbox. A per-mailbox commit would be
        # tidier to reason about but would leave a partially-advanced set of
        # cursors if the process died mid-run.
        await session.commit()

    for r in results:
        if r.skipped_reason:
            report.mailboxes_skipped += 1
            continue
        report.mailboxes_synced += 1
        report.captured += r.inserted
        report.unresolved += r.unresolved
        report.restricted += r.restricted
        report.errors.extend(f"{r.mailbox_address}: {e}" for e in r.errors)

    log.info("capture: %d new across %d mailbox(es), %d skipped, %d unresolved, %d restricted",
             report.captured, report.mailboxes_synced, report.mailboxes_skipped,
             report.unresolved, report.restricted)

    if not analyse:
        await _finish(run_id, report)
        return report

    # ── analyse ──────────────────────────────────────────────────────────────
    # Everything not yet done, not just this run's captures: a row whose analysis
    # failed earlier (an LLM outage, a bad key) is picked up here rather than
    # needing a separate backfill. That is why capture never depends on analysis
    # succeeding.
    async with SessionLocal() as session:
        cfg = await session.scalar(select(AgentConfig))
        auto_reply = bool(cfg and cfg.auto_reply_enabled)
        rows = (await session.execute(
            select(Email)
            .where(Email.summary_status != "done")
            .order_by(Email.received_at.desc())
            .limit(analyse_limit)
        )).scalars().all()
        from .schemas import serialize_email
        payloads = [(e.id, serialize_email(e)) for e in rows]

    if not payloads:
        await _finish(run_id, report)
        return report

    try:
        ok, high, _all_ok = await pipeline.analyze_and_persist(
            payloads,
            auto_reply_enabled=auto_reply,
            # Send credentials are now deployment configuration rather than a
            # per-mailbox row. The physical mailbox a reply goes out from is
            # resolved per email by send_identity.derive, not fixed here.
            send_creds={
                "tenant_id": settings.graph_tenant_id,
                "client_id": settings.graph_client_id,
                "client_secret": settings.graph_client_secret,
                "mailbox": "",
            },
            provider="graph",
        )
        report.analysed, report.high_priority = ok, high
    except Exception as exc:  # noqa: BLE001 - a failed analysis must not lose captures
        report.errors.append(f"analysis: {type(exc).__name__}: {exc}")
        log.exception("analysis phase FAILED (captures are already committed)")

    log.info("analysis: %d analysed, %d high priority", report.analysed, report.high_priority)
    await _finish(run_id, report)
    return report


async def _finish(run_id: str, report: SyncReport) -> None:
    """Close out the AgentRun row this sync opened.

    "partial" rather than "failed" when some mailboxes succeeded: one mailbox
    being throttled should not make a run that collected from the other five look
    like a total loss.
    """
    async with SessionLocal() as session:
        run = await session.get(AgentRun, run_id)
        if run is None:
            return
        run.fetched = report.captured
        run.processed = report.analysed
        run.high_priority = report.high_priority
        run.archived = 0
        if not report.errors:
            run.status = "success"
        elif report.mailboxes_synced:
            run.status = "partial"
        else:
            run.status = "failed"
        if report.errors:
            run.error_message = "; ".join(report.errors)[:1000]
        await session.commit()
