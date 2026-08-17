"""Fetch → dedup → insert(pending) → analyze via Agent Factory → done/failed → record run.

DB connections are held only for short transactions; the slow IMAP fetch and LLM
analysis run with no connection checked out, so they can't starve the pool.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from . import agent_client, billing_client, crypto, graph_client, mail_client
from .db import SessionLocal
from .models import AgentRun, Email, Mailbox
from .schemas import serialize_email

log = logging.getLogger("mail_agent.pipeline")


async def fetch_and_process(org_id: str, count: int, *, sweep: bool = False) -> list[str]:
    """Returns the ids of newly-fetched emails.

    Raises BillingBlocked (before any IMAP/SMTP work happens) if the org's
    subscription is inactive or this month's analysis quota is used up."""
    log.info("fetch start: org=%s count=%d sweep=%s", org_id, count, sweep)
    await billing_client.check_entitlement(org_id)

    # 1) short txn: read mailbox creds
    async with SessionLocal() as s:
        mailbox = await s.scalar(select(Mailbox).where(Mailbox.org_id == org_id))
        if not mailbox or not mailbox.enabled:
            log.warning("fetch aborted: org=%s has no enabled mailbox", org_id)
            raise LookupError("no mailbox configured for this org")
        provider = mailbox.provider
        if provider == "graph":
            creds = {
                "tenant_id": mailbox.tenant_id,
                "client_id": mailbox.client_id,
                "client_secret": crypto.decrypt(mailbox.client_secret_enc),
                "mailbox": mailbox.username,
            }
        else:
            creds = {
                "imap_host": mailbox.imap_host,
                "imap_port": mailbox.imap_port,
                "username": mailbox.username,
                "password": crypto.decrypt(mailbox.password_enc),
            }

    # 2) fetch — NO DB connection held
    if provider == "graph":
        log.info("fetch: connecting via Graph as %s", creds["mailbox"])
        messages = await run_in_threadpool(graph_client.fetch_latest, count=count, **creds)
    else:
        log.info("fetch: connecting IMAP %s:%s as %s", creds["imap_host"], creds["imap_port"], creds["username"])
        messages = await run_in_threadpool(mail_client.fetch_latest, count=count, **creds)
    log.info("fetch: pulled %d message(s) from INBOX", len(messages))

    # 3) short txn: sweep + dedup + insert pending; collect (id, payload) for new ones
    new: list[tuple[str, dict]] = []
    async with SessionLocal() as s:
        if sweep:
            now = datetime.now(timezone.utc)
            prior = list(await s.scalars(select(Email).where(Email.org_id == org_id, Email.archived_at.is_(None))))
            for e in prior:
                e.archived_at = now
                e.archived_by = "cron"
            if prior:
                log.info("sweep: archived %d prior email(s)", len(prior))
        for m in messages:
            if await s.scalar(select(Email).where(Email.org_id == org_id, Email.uid == m["uid"])):
                continue  # dedup
            row = Email(
                org_id=org_id, uid=m["uid"], sender=m["from"], from_email=m["fromEmail"],
                subject=m["subject"], body=m["body"], received_at=m["receivedAt"], summary_status="pending",
            )
            s.add(row)
            await s.flush()
            new.append((row.id, serialize_email(row)))
        await s.commit()
    log.info("fetch: %d new email(s) after dedup", len(new))

    # 4) analyze via Agent Factory — NO DB connection held
    sem = asyncio.Semaphore(5)

    async def analyze(email_id: str, payload: dict) -> tuple[str, dict, bool]:
        async with sem:
            try:
                res = await agent_client.analyze_email(payload)
                a = res.get("analysis") or {}
                log.info("analyzed uid=%s -> %s/%s", payload.get("uid"), a.get("category"), a.get("priority"))
                return email_id, a, True
            except Exception as exc:
                log.warning("analysis FAILED uid=%s: %s", payload.get("uid"), exc)
                return email_id, {}, False

    results = await asyncio.gather(*(analyze(eid, p) for eid, p in new))

    # 5) short txn: persist results + record the run
    high = 0
    async with SessionLocal() as s:
        for email_id, a, ok in results:
            e = await s.get(Email, email_id)
            if not e:
                continue
            if ok:
                e.summary = a.get("summary", "")
                e.category = a.get("category", "")
                e.priority = a.get("priority", "Medium")
                e.summary_status = "done"
                if e.priority == "High":
                    high += 1
            else:
                e.summary_status = "failed"
        all_ok = all(ok for _, _, ok in results)
        s.add(AgentRun(
            org_id=org_id, fetched=len(messages), processed=len(new),
            high_priority=high, status="success" if all_ok else "partial",
        ))
        await s.commit()
    log.info("run recorded: org=%s fetched=%d processed=%d high=%d", org_id, len(messages), len(new), high)

    analyzed_ok = sum(1 for _, _, ok in results if ok)
    await billing_client.record_usage(org_id, analyzed_ok)

    return [eid for eid, _ in new]
