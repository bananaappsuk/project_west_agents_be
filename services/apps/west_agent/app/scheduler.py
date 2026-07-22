"""Hourly cron: for each org with an enabled mailbox, sweep + fetch + process.

Off unless CRON_ENABLED=true. Manual POST /emails/fetch is the primary dev path.
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from . import pipeline
from .config import settings
from .db import SessionLocal
from .models import Mailbox

scheduler = AsyncIOScheduler()


async def _run_all() -> None:
    async with SessionLocal() as session:
        mailboxes = list(await session.scalars(select(Mailbox).where(Mailbox.enabled.is_(True))))
    for mb in mailboxes:
        async with SessionLocal() as session:
            try:
                await pipeline.fetch_and_process(session, mb.org_id, settings.fetch_per_run, sweep=True)
            except Exception:
                pass  # a failed run is recorded per-org; never crash the scheduler


def start() -> None:
    if not settings.cron_enabled:
        return
    scheduler.add_job(_run_all, "interval", minutes=settings.cron_interval_minutes,
                      id="mail_cron", replace_existing=True)
    scheduler.start()


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
