"""Fetch → sweep(new→old) → dedup → insert(pending) → analyse via Agent Factory →
done/failed → record run + notification.

DB connections are held only for short transactions; the BT Cloud fetch and the LLM
analysis run with no connection checked out, so they can't starve the pool.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy import update as sa_update

# agent_client and crm_sync are deliberately absent: the workflow is the only
# caller of either, and an import here would be the first step back towards a
# second submission path (AC-5, E-2).
from . import bt_client, s3_client, source_config
from .config import settings
from .db import SessionLocal
from .models import AgentRun, Notification, Recording

log = logging.getLogger("voice_agent.pipeline")

# In-memory guard against two overlapping runs for the same org — a manual "Sync
# now" click and a cron tick landing at the same moment, or a double-click. Only
# valid because this service runs as a single process (see run-all.ps1 — no
# --workers); the scheduler's own tick-pileup guard defers to this same set.
_running = False

# In-memory guard against two concurrent analyze attempts for the *same
# recording* — a manual retry (api.py's /recordings/{id}/retry-analysis)
# landing while this run's own analyze step (or another retry / a double-click)
# is still working on it. Without this, both calls independently write their
# own result and whichever commits last wins regardless of which one actually
# succeeded, so a retry that visibly succeeds could get silently overwritten
# back to "failed" moments later. Scoped per-recording, not per-org like
# `_running`, since analyzing unrelated recordings concurrently
# is fine.
_analyzing: set[str] = set()


def is_analyzing(rec_id: str) -> bool:
    return rec_id in _analyzing


def try_claim_analyzing(rec_id: str) -> bool:
    """Atomically checks-and-claims (safe under asyncio's cooperative scheduling
    since nothing here awaits) — returns False if already claimed elsewhere."""
    if rec_id in _analyzing:
        return False
    _analyzing.add(rec_id)
    return True


def release_analyzing(rec_id: str) -> None:
    _analyzing.discard(rec_id)


# A row stuck at analysis_status="pending" past this age was inserted by a run
# that never finished analyzing it (crashed process, killed connection, a very
# large batch that got interrupted) — since fetch only ever looks at ext_ids it
# hasn't seen before, such a row would otherwise stay "pending" forever. Every
# subsequent run sweeps these back in for another analysis attempt.
_STUCK_PENDING_AFTER = timedelta(minutes=10)


def is_running() -> bool:
    return _running


async def fetch_and_process(count: int, *, sweep: bool = False) -> list[str]:
    """Returns the ids of newly-fetched recordings. Raises RuntimeError if a run
    for this org is already in progress.

    Runs as a background task (see api.py's /recordings/fetch) rather than being
    awaited inline by an HTTP request — a full sync can take minutes on a large
    batch, far longer than any request should stay open. An `AgentRun` row is
    created up front with status="running" so any page can poll GET /voice/agent/status
    to see progress, regardless of which page issued the request or whether that
    page is even still mounted."""
    global _running
    if _running:
        raise RuntimeError("a sync is already in progress")
    _running = True

    async with SessionLocal() as s:
        run = AgentRun(status="running")
        s.add(run)
        await s.commit()
        run_id = run.id
    log.info("fetch start: count=%d sweep=%s run=%s", count, sweep, run_id)

    try:
        return await _run(count, sweep, run_id)
    except Exception as exc:
        log.exception("fetch FAILED: run=%s", run_id)
        async with SessionLocal() as s:
            r = await s.get(AgentRun, run_id)
            if r:
                r.status = "failed"
                r.error_message = str(exc)[:2000]
                await s.commit()
        raise
    finally:
        _running = False


_ORCHESTRATOR = None


def _orchestrator():
    """The service's orchestrator, built once.

    Cached because `build_orchestrator` assembles a handler registry and
    validates every definition, and a batch of twelve recordings would otherwise
    do that twelve times per tick.
    """
    global _ORCHESTRATOR
    if _ORCHESTRATOR is None:
        from .orchestration import RecordingAuthorizationPort, build_orchestrator

        _ORCHESTRATOR = build_orchestrator(
            RecordingAuthorizationPort(settings.app_key)
        )
    return _ORCHESTRATOR


async def _process_via_workflow(rec_id: str) -> bool:
    """Analyse one recording and forward it to the CRM, through the workflow.

    This is the only path from a recording to the CRM. `voice.submit_to_crm`
    lives inside `voice.process_intake`, and `crm_sync` has no other caller in
    application code -- which is AC-5's E-2, and is asserted by a test.

    Returns whether the recording ended up analysed, which is all `_run` needs
    to compute this batch's counts.

    Never raises. The pipeline's standing rule is that a downstream failure
    costs that row's status and nothing else, so a workflow that cannot start
    or cannot finish leaves `analysis_status` for the caller to mark and the
    rest of the batch unaffected.

    Safe to call twice over one recording: the idempotency key is the recording
    id, so a second call returns the run that already exists rather than
    submitting again. If that run is still in flight, this returns its current
    outcome rather than waiting on it.
    """
    from .orchestration import INTAKE, start_intake
    from platform_orchestration import SYSTEM_PRINCIPAL, WorkflowState

    try:
        orchestrator = _orchestrator()
        run = await start_intake(
            orchestrator, rec_id, principal=SYSTEM_PRINCIPAL,
        )
        if run.state in (WorkflowState.SUCCEEDED,):
            # An earlier tick already completed it; nothing more to do, and in
            # particular nothing to re-submit.
            log.info("workflow for %s already complete", rec_id)
            return True
        run = await orchestrator.run_to_completion(
            run.id, principal=SYSTEM_PRINCIPAL
        )
        if run.state is WorkflowState.SUCCEEDED:
            return True
        log.warning(
            "workflow %s for %s ended %s: %s",
            run.id, rec_id, run.state.value, run.last_error,
        )
        # A run that is waiting on a retry has not failed: the scheduler's
        # resume will carry it on, and marking the row "failed" now would hide
        # it from the retry it is already queued for.
        return run.state in (WorkflowState.RETRY_SCHEDULED,)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "workflow could not process %s: %s", rec_id, type(exc).__name__
        )
        return False


async def _run(count: int, sweep: bool, run_id: str) -> list[str]:
    # 1) the recording source, from deployment configuration. No database read and
    #    no decryption: see source_config.py for why this moved out of the
    #    voice_settings row. Refusing an unconfigured source here is what stops a
    #    half-configured deployment quietly storing BT Cloud's demo recordings.
    cfg = source_config.load()
    if not cfg.configured:
        log.warning("fetch aborted: %s", cfg.reason)
        raise LookupError(cfg.reason)

    source_type = cfg.source_type
    if source_type == source_config.S3:
        list_fn, transcribe_fn = s3_client.list_latest, s3_client.transcribe_one
    else:
        list_fn, transcribe_fn = bt_client.list_latest, bt_client.transcribe_one
    creds = cfg.creds
    transcribe_creds = cfg.transcribe_creds

    # 2) list metadata from the source — NO DB connection held, NO audio download yet.
    # Downloading + transcribing happens below, in step 3.5, and only for recordings this
    # org doesn't already have — a call already synced before is never re-downloaded or
    # re-transcribed just because it still shows up in a fresh listing.
    recordings = await run_in_threadpool(list_fn, count, **creds)
    log.info("fetch: listed %d recording(s) via %s", len(recordings), source_type)

    # 3) short, read-only txn: dedup (metadata only, no transcript yet). The sweep
    # (archiving the prior "new" batch) deliberately does NOT happen here — it's
    # deferred to step 4, in the same transaction as the insert, so that if this
    # run's insert fails for any reason, the archive never took effect either. A
    # run that ends up saving 0 recordings must never still have archived the
    # previous batch on its way to failing.
    async with SessionLocal() as s:
        # One batched query instead of a per-recording SELECT, and an in-memory
        # `seen` set to also catch two recordings sharing an ext_id *within this
        # same fetch* (e.g. a source returning an overlapping page) — the old
        # per-item DB check only looked at what was already stored, so an
        # intra-batch duplicate passed the check for both, and the second
        # `s.add(row)` / `s.flush()` below raised an uncaught IntegrityError that
        # rolled back every already-flushed recording in this transaction.
        fetched_ext_ids = [m["ext_id"] for m in recordings]
        known_ext_ids = set(await s.scalars(
            select(Recording.ext_id).where(Recording.ext_id.in_(fetched_ext_ids))
        )) if fetched_ext_ids else set()
        seen_ext_ids: set[str] = set()
        fresh = []
        for m in recordings:
            eid = m["ext_id"]
            if eid in known_ext_ids or eid in seen_ext_ids:
                continue
            seen_ext_ids.add(eid)
            fresh.append(m)
    log.info("fetch: %d new recording(s) after dedup", len(fresh))

    # 3.5) transcribe — NO DB connection held, and only for items dedup didn't already
    # drop. This is the whole point of splitting list/transcribe: a call already synced
    # in an earlier run is never downloaded or sent to Whisper again just because it's
    # still within the source's listing window. Demo items already carry a canned
    # transcript from list_latest, so they skip this entirely.
    transcribe_sem = asyncio.Semaphore(5)

    async def fill_transcript(m: dict) -> dict:
        if m.get("transcript"):
            return m
        async with transcribe_sem:
            m["transcript"] = await run_in_threadpool(transcribe_fn, m, **transcribe_creds)
            return m

    fresh = await asyncio.gather(*(fill_transcript(m) for m in fresh))

    # 4) short txn: sweep prior 'new' -> 'old' (only if this run actually has
    # something fresh to replace them with — see step 3's comment), THEN insert
    # the newly-fetched (now-transcribed) recordings, in the SAME transaction —
    # one commit covers both, so a failure anywhere in this block (e.g. an insert
    # error) rolls the sweep back too, instead of archiving the previous batch
    # on the way to saving zero new recordings.
    new: list[tuple[str, dict]] = []
    async with SessionLocal() as s:
        if sweep and fresh:
            # A bulk UPDATE, not a SELECT-then-mutate — the earlier ORM-load form
            # pulled every "new" recording's full transcript/summary/ai_reply text
            # over the wire just to flip one status column, which is what was
            # driving up Neon's data-transfer usage on every sweep.
            result = await s.execute(
                sa_update(Recording)
                .where(Recording.status == "new")
                .values(status="old")
            )
            if result.rowcount and result.rowcount > 0:
                log.info("sweep: moved %d recording(s) new -> old", result.rowcount)

        for m in fresh:
            row = Recording(
                ext_id=m["ext_id"], source_type=source_type, caller=m["caller"], phone=m["phone"],
                agent=m["agent"], call_date=m["date"], duration=m["duration"],
                transcript=m["transcript"], content_uri=m.get("content_uri"),
                transcript_segments=m.get("transcript_segments"),
                status="new", analysis_status="pending",
            )
            s.add(row)
            await s.flush()
            new.append((row.id, {
                "ext_id": row.ext_id, "call_date": row.call_date,
                "caller": row.caller, "phone": row.phone, "agent": row.agent,
                "duration": row.duration, "transcript": row.transcript,
            }))

        # Self-heal: a row stuck "pending" from a run that never finished analyzing
        # it (see _STUCK_PENDING_AFTER) would otherwise be invisible to every future
        # fetch — dedup only looks at *new* ext_ids. Re-queue it here using its
        # already-stored transcript, no need to re-fetch/re-transcribe it.
        stale_cutoff = datetime.now(timezone.utc) - _STUCK_PENDING_AFTER
        stuck = list(await s.scalars(
            select(Recording).where( Recording.analysis_status == "pending",
                Recording.created_at < stale_cutoff,
            )
        ))
        if stuck:
            log.info("re-queuing %d stuck-pending recording(s) from an earlier interrupted run", len(stuck))
            new.extend((r.id, {
                "ext_id": r.ext_id, "call_date": r.call_date,
                "caller": r.caller, "phone": r.phone, "agent": r.agent,
                "duration": r.duration, "transcript": r.transcript,
            }) for r in stuck)

        await s.commit()
    log.info("fetch: %d recording(s) inserted/re-queued for analysis", len(new))

    # 4) analyse via Agent Factory — NO DB connection held
    sem = asyncio.Semaphore(5)
    payload_by_id = dict(new)

    # Skip (leave "pending") any recording already claimed by a concurrent
    # analyze attempt — a manual retry (api.py's /recordings/{id}/retry-analysis)
    # landing on the same recording this run just re-queued (see `_analyzing`'s
    # docstring). Only ids actually claimed here get released in the `finally`
    # below, so a skipped id is left for whichever call already owns it to finish.
    to_analyze = [(rid, p) for rid, p in new if try_claim_analyzing(rid)]
    claimed = [rid for rid, _ in to_analyze]

    # Analysis and CRM submission both happen inside the orchestration workflow
    # now (Ticket 3 AC-5). This function no longer calls the Agent Factory and no
    # longer calls `crm_sync`: `voice.submit_to_crm` is the single submission
    # path, which is what AC-5's E-2 asks for and what makes the approval gate,
    # the retry policy, the lease and the audit trail apply to every submission
    # rather than only to the ones that happen to go through a workflow.
    #
    # What has NOT changed, deliberately:
    #
    #   * the `_analyzing` claim above still skips a recording a manual retry is
    #     already working on, so the two cannot race each other in-process;
    #   * the workflow is started with the recording id as its idempotency key,
    #     so a second tick over the same row returns the existing run;
    #   * `AgentRun`'s counts are still computed here, from the rows the workflow
    #     persisted, so the dashboard reads exactly as it did;
    #   * the stuck-pending re-queue above is untouched.
    #
    # A slow CRM call still holds no database connection: the workflow's own
    # steps open and close their own sessions, and nothing here is inside the
    # persist transaction below.
    async def process(rec_id: str):
        async with sem:
            return rec_id, await _process_via_workflow(rec_id)

    try:
        outcomes = await asyncio.gather(*(process(rid) for rid, _ in to_analyze))
        results = [(rec_id, {}, ok) for rec_id, ok in outcomes]

        # 5) short txn: mark a failed analysis, record the run, notify.
        #
        # The analysis fields and the CRM outcome are no longer written here --
        # `voice.process_intake` persisted them inside the workflow, in one
        # transaction, exactly as this block used to. What is left is the two
        # things the workflow does not know about: this batch's `AgentRun`
        # counts, and the notification.
        #
        # `high_risk` is therefore read back from the rows rather than counted
        # from an in-memory analysis. Same number, one query, and it stays
        # correct if a workflow retried and changed the risk on a later attempt.
        high = 0
        async with SessionLocal() as s:
            for rec_id, _a, ok in results:
                r = await s.get(Recording, rec_id)
                if not r:
                    continue
                if ok:
                    if r.risk == "High":
                        high += 1
                else:
                    # The workflow could not analyse it. Marked here rather than
                    # in the handler so a run that never got as far as running --
                    # refused authorization, say -- still leaves the row in a
                    # state the next sweep can see.
                    r.analysis_status = "failed"
            all_ok = all(ok for _, _, ok in results)
            run = await s.get(AgentRun, run_id)
            if run:
                run.fetched, run.processed, run.high_risk = len(recordings), len(new), high
                run.status = "success" if all_ok else "partial"
            if new:
                source_label = "an S3-compatible bucket" if source_type == "s3" else "BT Cloud"
                s.add(Notification(text=f"{len(new)} new recordings fetched from {source_label}"))
            await s.commit()
    finally:
        for rid in claimed:
            release_analyzing(rid)

    log.info("run recorded: fetched=%d processed=%d high_risk=%d", len(recordings), len(new), high)
    return [rid for rid, _ in new]
