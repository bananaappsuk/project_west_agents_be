"""Background scheduler, driven by each org's VoiceSettings (BT Cloud cron config).

A tick fires every CRON_TICK_MINUTES. On each tick, for every org whose VoiceSettings
is enabled (and has BT Cloud creds), it runs the fetch pipeline **if** enough time has
elapsed since that org's last run to satisfy its chosen frequency. CRON_ENABLED is the
master on/off switch.
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
        enabled = list(await session.scalars(
            select(VoiceSettings).where(VoiceSettings.enabled.is_(True))
        ))

    log.info("tick: %d org(s) with a BT Cloud connection enabled", len(enabled))
    now = datetime.now(timezone.utc)

    for cfg in enabled:
        if pipeline.is_running(cfg.org_id):
            log.info("org=%s previous run still in progress — skipping", cfg.org_id)
            continue
        if not cfg.endpoint:
            log.info("org=%s enabled but no endpoint configured — skipping", cfg.org_id)
            continue

        async with SessionLocal() as session:
            last_run = await session.scalar(
                select(func.max(AgentRun.run_at)).where(AgentRun.org_id == cfg.org_id)
            )
            interval = _interval_minutes(cfg.cron_frequency)
            if last_run is not None:
                if last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=timezone.utc)
                if (now - last_run) < timedelta(minutes=interval):
                    due_in = interval - int((now - last_run).total_seconds() // 60)
                    log.info("org=%s not due yet (interval=%dm, ~%dm remaining)", cfg.org_id, interval, due_in)
                    continue

        # pipeline.py owns the running-guard and already logs + records failure on
        # the AgentRun row itself — this catch just keeps one org's failure from
        # stopping the tick loop from reaching the rest.
        log.info("org=%s DUE — running fetch (interval=%dm)", cfg.org_id, interval)
        try:
            new = await pipeline.fetch_and_process(cfg.org_id, settings.fetch_per_run, sweep=True)
            log.info("org=%s cron run complete — %d new recording(s)", cfg.org_id, len(new))
        except Exception:
            log.exception("org=%s cron run FAILED", cfg.org_id)


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
