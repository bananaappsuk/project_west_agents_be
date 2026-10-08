"""Background scheduler, driven by Settings (AgentConfig).

A tick fires every CRON_TICK_MINUTES. On each tick, if the agent is enabled and a
mailbox is configured, it runs the fetch pipeline **if** enough time has elapsed
since the last run to satisfy the chosen interval. CRON_ENABLED is the master
on/off switch.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select

from . import pipeline, sync
from .config import settings
from .db import SessionLocal
from .models import AgentConfig, AgentRun, Mailbox

log = logging.getLogger("mail_agent.scheduler")
scheduler = AsyncIOScheduler()

_INTERVAL_MINUTES = {
    "Every 15 minutes": 15,
    "Every 30 minutes": 30,
    "Every hour": 60,
    "Every 6 hours": 360,
    "Daily": 1440,
}


def _interval_minutes(label: str) -> int:
    return _INTERVAL_MINUTES.get(label, 60)


async def _tick() -> None:
    async with SessionLocal() as session:
        cfg = await session.scalar(select(AgentConfig))

    if cfg is None or not cfg.enabled:
        log.info("tick: agent not enabled — nothing to do")
        return

    if pipeline.is_running():
        log.info("previous run still in progress — skipping this tick")
        return

    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        # Any ENABLED Project West mailbox is enough to make the tick worth running.
        # Eligibility per mailbox (ingestion boundary, Graph id) is decided inside
        # the sync itself, which reports skips rather than silently doing nothing.
        mailbox = await session.scalar(
            select(Mailbox).where(Mailbox.enabled.is_(True), Mailbox.kind == "pw")
        )
        if not mailbox:
            log.info("agent enabled but no Project West mailbox configured — skipping")
            return

        last_run = await session.scalar(select(func.max(AgentRun.run_at)))
        interval = _interval_minutes(cfg.cron_interval)
        if last_run is not None and (now - last_run) < timedelta(minutes=interval):
            due_in = interval - int((now - last_run).total_seconds() // 60)
            log.info("not due yet (interval=%dm, ~%dm remaining)", interval, due_in)
            return

    # DB connection released before the slow fetch + LLM work. pipeline.py owns
    # the running-guard and already logs + records failure on the AgentRun row
    # itself — this catch just keeps a failure from killing the scheduler job.
    log.info("DUE — running fetch (interval=%dm)", interval)
    try:
        # Multi-mailbox: captures from every eligible Project West mailbox, then
        # analyses. NOT pipeline.fetch_and_process, which selects a single mailbox
        # with a bare scalar() and was only correct while exactly one existed.
        report = await sync.run_once()
        log.info("cron run complete — captured=%d analysed=%d across %d mailbox(es), "
                 "%d skipped, %d unresolved, %d restricted%s",
                 report.captured, report.analysed, report.mailboxes_synced,
                 report.mailboxes_skipped, report.unresolved, report.restricted,
                 f", errors={report.errors}" if report.errors else "")
    except Exception:
        log.exception("cron run FAILED")


def start() -> None:
    if not settings.cron_enabled:
        log.info("CRON_ENABLED=false — scheduler NOT started")
        return
    scheduler.add_job(_tick, "interval", minutes=settings.cron_tick_minutes,
                      id="mail_cron_tick", replace_existing=True)
    scheduler.start()
    log.info("cron scheduler started — tick every %d min", settings.cron_tick_minutes)


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("cron scheduler stopped")
