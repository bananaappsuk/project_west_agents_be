"""Fetch → sweep(new→old) → dedup → insert(pending) → analyse via Agent Factory →
done/failed → record run + notification.

DB connections are held only for short transactions; the BT Cloud fetch and the LLM
analysis run with no connection checked out, so they can't starve the pool.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from . import agent_client, bt_client, crypto, s3_client
from .db import SessionLocal
from .models import AgentRun, Notification, Recording, VoiceSettings

log = logging.getLogger("voice_agent.pipeline")


async def fetch_and_process(org_id: str, count: int, *, sweep: bool = False) -> list[str]:
    """Returns the ids of newly-fetched recordings."""
    log.info("fetch start: org=%s count=%d sweep=%s", org_id, count, sweep)

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

    # 3) short txn: sweep prior 'new' -> 'old', dedup + insert pending
    new: list[tuple[str, dict]] = []
    async with SessionLocal() as s:
        if sweep:
            prior = list(await s.scalars(
                select(Recording).where(Recording.org_id == org_id, Recording.status == "new")
            ))
            for r in prior:
                r.status = "old"
            if prior:
                log.info("sweep: moved %d recording(s) new -> old", len(prior))
        for m in recordings:
            if await s.scalar(select(Recording).where(Recording.org_id == org_id, Recording.ext_id == m["ext_id"])):
                continue  # dedup
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
            else:
                r.analysis_status = "failed"
        all_ok = all(ok for _, _, ok in results)
        s.add(AgentRun(
            org_id=org_id, fetched=len(recordings), processed=len(new),
            high_risk=high, status="success" if all_ok else "partial",
        ))
        if new:
            source_label = "an S3-compatible bucket" if source_type == "s3" else "BT Cloud"
            s.add(Notification(org_id=org_id, text=f"{len(new)} new recordings fetched from {source_label}"))
        await s.commit()
    log.info("run recorded: org=%s fetched=%d processed=%d high_risk=%d", org_id, len(recordings), len(new), high)
    return [rid for rid, _ in new]
