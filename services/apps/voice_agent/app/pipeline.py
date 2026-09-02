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

from . import agent_client, bt_client, crm_sync, crypto, s3_client
from .db import SessionLocal
from .models import AgentRun, Notification, Recording, VoiceSettings

log = logging.getLogger("voice_agent.pipeline")

# In-memory guard against two overlapping runs for the same org — a manual "Sync
# now" click and a cron tick landing at the same moment, or a double-click. Only
# valid because this service runs as a single process (see run-all.ps1 — no
# --workers); the scheduler's own tick-pileup guard defers to this same set.
_running: set[str] = set()

# A row stuck at analysis_status="pending" past this age was inserted by a run
# that never finished analyzing it (crashed process, killed connection, a very
# large batch that got interrupted) — since fetch only ever looks at ext_ids it
# hasn't seen before, such a row would otherwise stay "pending" forever. Every
# subsequent run sweeps these back in for another analysis attempt.
_STUCK_PENDING_AFTER = timedelta(minutes=10)


def is_running(org_id: str) -> bool:
    return org_id in _running


async def fetch_and_process(org_id: str, count: int, *, sweep: bool = False) -> list[str]:
    """Returns the ids of newly-fetched recordings. Raises RuntimeError if a run
    for this org is already in progress.

    Runs as a background task (see api.py's /recordings/fetch) rather than being
    awaited inline by an HTTP request — a full sync can take minutes on a large
    batch, far longer than any request should stay open. An `AgentRun` row is
    created up front with status="running" so any page can poll GET /voice/agent/status
    to see progress, regardless of which page issued the request or whether that
    page is even still mounted."""
    if org_id in _running:
        raise RuntimeError(f"a sync is already in progress for org={org_id}")
    _running.add(org_id)

    async with SessionLocal() as s:
        run = AgentRun(org_id=org_id, status="running")
        s.add(run)
        await s.commit()
        run_id = run.id
    log.info("fetch start: org=%s count=%d sweep=%s run=%s", org_id, count, sweep, run_id)

    try:
        return await _run(org_id, count, sweep, run_id)
    except Exception as exc:
        log.exception("fetch FAILED: org=%s run=%s", org_id, run_id)
        async with SessionLocal() as s:
            r = await s.get(AgentRun, run_id)
            if r:
                r.status = "failed"
                r.error_message = str(exc)[:2000]
                await s.commit()
        raise
    finally:
        _running.discard(org_id)


async def _run(org_id: str, count: int, sweep: bool, run_id: str) -> list[str]:
    # 1) short txn: read the org's connection settings for whichever source is active
    async with SessionLocal() as s:
        cfg = await s.scalar(select(VoiceSettings).where(VoiceSettings.org_id == org_id))
        if not cfg or not cfg.enabled:
            log.warning("fetch aborted: org=%s has no enabled recording source connection", org_id)
            raise LookupError("no recording source configured for this org")
        source_type = cfg.source_type
        if source_type == "s3":
            fetch_fn = s3_client.fetch_latest
            creds = {
                "endpoint": cfg.s3_endpoint,
                "region": cfg.s3_region,
                "bucket": cfg.s3_bucket,
                "prefix": cfg.s3_prefix,
                "access_key_id": cfg.s3_access_key_id,
                "secret_access_key": crypto.decrypt(cfg.s3_secret_access_key_enc) if cfg.s3_secret_access_key_enc else "",
            }
        else:
            fetch_fn = bt_client.fetch_latest
            creds = {
                "endpoint": cfg.endpoint,
                "client_id": cfg.client_id,
                "client_secret": crypto.decrypt(cfg.client_secret_enc) if cfg.client_secret_enc else "",
                "jwt": crypto.decrypt(cfg.jwt_enc) if cfg.jwt_enc else "",
            }

    # 2) fetch from the source — NO DB connection held
    recordings = await run_in_threadpool(fetch_fn, count, **creds)
    log.info("fetch: pulled %d recording(s) via %s", len(recordings), source_type)

    # 3) short txn: dedup, then sweep prior 'new' -> 'old' only if this run actually
    # has something to replace them with — an empty/all-duplicate fetch (source
    # temporarily returned nothing, or a re-run within the same window) must never
    # archive the existing "new" bucket with nothing to show for it.
    new: list[tuple[str, dict]] = []
    async with SessionLocal() as s:
        fresh = [
            m for m in recordings
            if not await s.scalar(select(Recording).where(Recording.org_id == org_id, Recording.ext_id == m["ext_id"]))
        ]

        if sweep and fresh:
            # A bulk UPDATE, not a SELECT-then-mutate — the earlier ORM-load form
            # pulled every "new" recording's full transcript/summary/ai_reply text
            # over the wire just to flip one status column, which is what was
            # driving up Neon's data-transfer usage on every sweep.
            result = await s.execute(
                sa_update(Recording)
                .where(Recording.org_id == org_id, Recording.status == "new")
                .values(status="old")
            )
            if result.rowcount:
                log.info("sweep: moved %d recording(s) new -> old", result.rowcount)

        for m in fresh:
            row = Recording(
                org_id=org_id, ext_id=m["ext_id"], source_type=source_type, caller=m["caller"], phone=m["phone"],
                agent=m["agent"], call_date=m["date"], duration=m["duration"],
                transcript=m["transcript"], status="new", analysis_status="pending",
            )
            s.add(row)
            await s.flush()
            new.append((row.id, {
                "caller": row.caller, "phone": row.phone, "agent": row.agent,
                "duration": row.duration, "transcript": row.transcript,
            }))

        # Self-heal: a row stuck "pending" from a run that never finished analyzing
        # it (see _STUCK_PENDING_AFTER) would otherwise be invisible to every future
        # fetch — dedup only looks at *new* ext_ids. Re-queue it here using its
        # already-stored transcript, no need to re-fetch/re-transcribe it.
        stale_cutoff = datetime.now(timezone.utc) - _STUCK_PENDING_AFTER
        stuck = list(await s.scalars(
            select(Recording).where(
                Recording.org_id == org_id, Recording.analysis_status == "pending",
                Recording.created_at < stale_cutoff,
            )
        ))
        if stuck:
            log.info("re-queuing %d stuck-pending recording(s) from an earlier interrupted run", len(stuck))
            new.extend((r.id, {
                "caller": r.caller, "phone": r.phone, "agent": r.agent,
                "duration": r.duration, "transcript": r.transcript,
            }) for r in stuck)

        await s.commit()
    log.info("fetch: %d new recording(s) after dedup", len(new))

    # 4) analyse via Agent Factory — NO DB connection held
    sem = asyncio.Semaphore(5)

    async def analyze(rec_id: str, payload: dict):
        async with sem:
            try:
                res = await agent_client.analyze_call(payload)
                return rec_id, (res.get("analysis") or {}), True
            except Exception as exc:
                log.warning("analysis FAILED rec=%s: %s", rec_id, exc)
                return rec_id, {}, False

    results = await asyncio.gather(*(analyze(rid, p) for rid, p in new))

    # 5) short txn: persist analysis + record the run + notification
    high = 0
    async with SessionLocal() as s:
        for rec_id, a, ok in results:
            r = await s.get(Recording, rec_id)
            if not r:
                continue
            if ok:
                r.summary = a.get("summary", "")
                r.category = a.get("category", "General Enquiry")
                r.priority = a.get("priority", "Medium")
                r.risk = a.get("risk", "Low")
                r.sentiment = a.get("sentiment", "Neutral")
                r.needs_reply = bool(a.get("needs_reply", False))
                r.ai_reply = a.get("suggested_reply", "") or ""
                r.reply_status = "pending" if r.needs_reply else "none"
                r.analysis_status = "done"
                if r.risk == "High":
                    high += 1
                r.crm_status, r.crm_reference = await crm_sync.after_call_analysis(r, a)
            else:
                r.analysis_status = "failed"
        all_ok = all(ok for _, _, ok in results)
        run = await s.get(AgentRun, run_id)
        if run:
            run.fetched, run.processed, run.high_risk = len(recordings), len(new), high
            run.status = "success" if all_ok else "partial"
        if new:
            source_label = "an S3-compatible bucket" if source_type == "s3" else "BT Cloud"
            s.add(Notification(org_id=org_id, text=f"{len(new)} new recordings fetched from {source_label}"))
        await s.commit()
    log.info("run recorded: org=%s fetched=%d processed=%d high_risk=%d", org_id, len(recordings), len(new), high)
    return [rid for rid, _ in new]
