"""Background scheduler, driven by each org's Settings (AgentConfig).

A tick fires every CRON_TICK_MINUTES. On each tick, for every org whose
AgentConfig.enabled is true and that has an enabled mailbox, it runs the fetch
pipeline **if** enough time has elapsed since that org's last run to satisfy its
chosen interval. CRON_ENABLED is the master on/off switch.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select

from . import pipeline
from .config import settings
from .db import SessionLocal
from .models import AgentConfig, AgentRun, Mailbox

log = logging.getLogger("mail_agent.scheduler")
scheduler = AsyncIOScheduler()
_running: set[str] = set()  # org_ids with a run in flight — prevents tick pileup

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
        enabled = list(await session.scalars(select(AgentConfig).where(AgentConfig.enabled.is_(True))))

    log.info("tick: %d org(s) with the agent enabled", len(enabled))
    now = datetime.now(timezone.utc)

    for cfg in enabled:
        if cfg.org_id in _running:
            log.info("org=%s previous run still in progress — skipping this tick", cfg.org_id)
            continue

        async with SessionLocal() as session:
            mailbox = await session.scalar(
                select(Mailbox).where(Mailbox.org_id == cfg.org_id, Mailbox.enabled.is_(True))
            )
            if not mailbox:
                log.info("org=%s enabled but no mailbox configured — skipping", cfg.org_id)
                continue

            last_run = await session.scalar(
                select(func.max(AgentRun.run_at)).where(AgentRun.org_id == cfg.org_id)
            )
            interval = _interval_minutes(cfg.cron_interval)
            if last_run is not None and (now - last_run) < timedelta(minutes=interval):
                due_in = interval - int((now - last_run).total_seconds() // 60)
                log.info("org=%s not due yet (interval=%dm, ~%dm remaining)", cfg.org_id, interval, due_in)
                continue

        # DB connection released before the slow fetch + LLM work
        _running.add(cfg.org_id)
        log.info("org=%s DUE — running fetch (interval=%dm)", cfg.org_id, interval)
        try:
            new = await pipeline.fetch_and_process(cfg.org_id, settings.fetch_per_run, sweep=True)
            log.info("org=%s cron run complete — %d new email(s)", cfg.org_id, len(new))
        except Exception:
            log.exception("org=%s cron run FAILED", cfg.org_id)
        finally:
            _running.discard(cfg.org_id)


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
