"""Background scheduler, driven by VoiceSettings (BT Cloud cron config).

A tick fires every CRON_TICK_MINUTES. On each tick, if VoiceSettings is enabled (and
has BT Cloud creds), it runs the fetch pipeline **if** enough time has elapsed since
the last run to satisfy the chosen frequency. CRON_ENABLED is the master on/off
switch.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select

from . import pipeline
from .config import settings
from .db import SessionLocal
from .models import AgentRun, VoiceSettings

log = logging.getLogger("voice_agent.scheduler")
scheduler = AsyncIOScheduler()

_FREQUENCY_MINUTES = {
    "hourly": 60,
    "every6h": 360,
    "daily": 1440,
}


def _interval_minutes(freq: str) -> int:
    return _FREQUENCY_MINUTES.get(freq, 360)


async def _tick() -> None:
    async with SessionLocal() as session:
        cfg = await session.scalar(select(VoiceSettings))

    if cfg is None or not cfg.enabled:
        log.info("tick: no enabled recording source — nothing to do")
        return
    if pipeline.is_running():
        log.info("previous run still in progress — skipping this tick")
        return
    if not cfg.endpoint:
        log.info("enabled but no endpoint configured — skipping")
        return

    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        last_run = await session.scalar(select(func.max(AgentRun.run_at)))
        interval = _interval_minutes(cfg.cron_frequency)
        if last_run is not None:
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=timezone.utc)
            if (now - last_run) < timedelta(minutes=interval):
                due_in = interval - int((now - last_run).total_seconds() // 60)
                log.info("not due yet (interval=%dm, ~%dm remaining)", interval, due_in)
                return

    # pipeline.py owns the running-guard and already logs + records failure on the
    # AgentRun row itself — this catch just keeps a failure from killing the job.
    log.info("DUE — running fetch (interval=%dm)", interval)
    try:
        new = await pipeline.fetch_and_process(settings.fetch_per_run, sweep=True)
        log.info("cron run complete — %d new recording(s)", len(new))
    except Exception:
        log.exception("cron run FAILED")


def start() -> None:
    if not settings.cron_enabled:
        log.info("CRON_ENABLED=false — scheduler NOT started")
        return
    scheduler.add_job(_tick, "interval", minutes=settings.cron_tick_minutes,
                      id="voice_cron_tick", replace_existing=True)
    scheduler.start()
    log.info("cron scheduler started — tick every %d min", settings.cron_tick_minutes)


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("cron scheduler stopped")
