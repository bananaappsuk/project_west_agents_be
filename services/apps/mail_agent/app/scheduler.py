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

from platform_orchestration import SYSTEM_PRINCIPAL

from . import orchestration, pipeline, sync
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


async def _workflow_tick() -> None:
    """Advance workflows that are waiting on the clock rather than on a person.

    Two sweeps, in this order:

      * `recover_stale` reclaims runs abandoned mid-step by a worker that died.
        A lapsed lease means no process is working on the run any more, so it is
        put back to `retry_scheduled` -- which keeps one code path for "try this
        step again" rather than a second, less-tested one for crash recovery.
      * `resume_due` then advances everything whose retry time has arrived,
        including what the first sweep just rescheduled.
      * `resume_policy_gated` releases autonomous replies parked at their policy
        gate with nothing left to approve them. Neither sweep above reaches a
        run in `awaiting_approval`, which is right for a run waiting on a person
        and wrong for one waiting on the org's auto-reply setting: the authority
        already exists, so a process that died between starting the run and
        approving it would otherwise strand it forever.

    Runs under the system principal, which the engine refuses the *human*
    approval path to outright -- so this tick can drive a workflow up to a human
    gate and never through one. `resume_policy_gated` is scoped to
    `mail.auto_reply`, whose send step opted in with `policy_approval=True`;
    a `mail.reply_approval` run waiting for a reviewer is untouched by all three.
    """
    orch = orchestration.build_orchestrator(
        orchestration.SystemAuthorizationPort()
    )
    try:
        recovered = await orch.recover_stale(principal=SYSTEM_PRINCIPAL)
        resumed = await orch.resume_due(principal=SYSTEM_PRINCIPAL)
        released = await orch.resume_policy_gated(
            principal=SYSTEM_PRINCIPAL,
            policy=orchestration.AUTO_REPLY_POLICY,
            definition=orchestration.AUTO_REPLY,
        )
    except Exception:
        # A failing sweep must not kill the scheduler job; the next tick retries.
        log.exception("workflow sweep FAILED")
        return
    if recovered or resumed or released:
        log.info("workflow sweep — %d recovered, %d resumed, %d policy-released",
                 len(recovered), len(resumed), len(released))


def start() -> None:
    if not settings.cron_enabled:
        log.info("CRON_ENABLED=false — scheduler NOT started")
        return
    scheduler.add_job(_tick, "interval", minutes=settings.cron_tick_minutes,
                      id="mail_cron_tick", replace_existing=True)
    # A separate job from the mail sync, on its own schedule. Workflow recovery
    # must not wait on the fetch interval (which can be hourly or daily): a reply
    # whose send hit a transient failure should retry in minutes, and tying it to
    # the sync would also mean a long-running fetch delayed every pending retry.
    scheduler.add_job(_workflow_tick, "interval",
                      minutes=settings.workflow_tick_minutes,
                      id="mail_workflow_tick", replace_existing=True)
    scheduler.start()
    log.info("cron scheduler started — mail tick every %d min, "
             "workflow tick every %d min",
             settings.cron_tick_minutes, settings.workflow_tick_minutes)


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("cron scheduler stopped")
