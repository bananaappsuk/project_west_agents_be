"""Fetch → dedup → insert(pending) → analyze via Agent Factory → done/failed → record run."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import agent_client, crypto, mail_client
from .models import AgentRun, Email, Mailbox
from .schemas import serialize_email


async def fetch_and_process(session: AsyncSession, org_id: str, count: int, *, sweep: bool = False) -> list[Email]:
    mailbox = await session.scalar(select(Mailbox).where(Mailbox.org_id == org_id))
    if not mailbox or not mailbox.enabled:
        raise LookupError("no mailbox configured for this org")

    password = crypto.decrypt(mailbox.password_enc)
    messages = await run_in_threadpool(
        mail_client.fetch_latest,
        imap_host=mailbox.imap_host,
        imap_port=mailbox.imap_port,
        username=mailbox.username,
        password=password,
        count=count,
    )

    # cron sweep: everything still in Mails from prior runs is archived (folder_id/draft untouched)
    if sweep:
        now = datetime.now(timezone.utc)
        prior = await session.scalars(select(Email).where(Email.org_id == org_id, Email.archived_at.is_(None)))
        for e in prior:
            e.archived_at = now
            e.archived_by = "cron"

    new_emails: list[Email] = []
    for m in messages:
        if await session.scalar(select(Email).where(Email.org_id == org_id, Email.uid == m["uid"])):
            continue  # dedup
        email_row = Email(
            org_id=org_id,
            uid=m["uid"],
            sender=m["from"],
            from_email=m["fromEmail"],
            subject=m["subject"],
            body=m["body"],
            received_at=m["receivedAt"],
            summary_status="pending",
        )
        session.add(email_row)
        new_emails.append(email_row)
    await session.commit()  # mail lands even if the LLM later fails

    # analyze concurrently (bounded)
    sem = asyncio.Semaphore(5)

    async def analyze(e: Email) -> None:
        async with sem:
            try:
                result = await agent_client.analyze_email(serialize_email(e))
                a = result.get("analysis") or {}
                e.summary = a.get("summary", "")
                e.category = a.get("category", "")
                e.priority = a.get("priority", "Medium")
                e.summary_status = "done"
            except Exception:
                e.summary_status = "failed"

    await asyncio.gather(*(analyze(e) for e in new_emails))
    await session.commit()

    high = sum(1 for e in new_emails if e.priority == "High")
    ok = all(e.summary_status == "done" for e in new_emails)
    session.add(
        AgentRun(
            org_id=org_id,
            fetched=len(messages),
            processed=len(new_emails),
            high_priority=high,
            status="success" if ok else "partial",
        )
    )
    await session.commit()
    return new_emails
