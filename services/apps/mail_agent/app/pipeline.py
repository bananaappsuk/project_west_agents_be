"""Fetch → dedup → insert(skipped) → auto-analyze the latest slice → done/failed → record run.

DB connections are held only for short transactions; the slow IMAP fetch and LLM
analysis run with no connection checked out, so they can't starve the pool.

Fetching (import) and analyzing (the LLM call) are deliberately decoupled. A
first-ever sync on a mailbox with tens of thousands of emails would otherwise
mean tens of thousands of sequential LLM calls before anything is even usable —
hours of blocking, real cost, for history nobody's asked to see yet. Instead:
every fetched email is inserted immediately (fast, cheap, so the whole backlog
imports quickly and gracefully — see `_run`'s batching), but only the most
recent `_AUTO_ANALYZE_COUNT` not-yet-analyzed ones get an AI summary
automatically. Older ones sit at summary_status="skipped" — available on demand
via `analyze_backlog` (Settings' "Summarize previous 100") or a per-email retry
(api.py's /emails/{id}/retry-summary), never forced on the user.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import nh3
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import and_, or_, select
from sqlalchemy import update as sa_update

from . import agent_client, crm_sync, crypto, graph_client, mail_client, sendgrid_client
from .config import settings
from .db import SessionLocal
from .models import AgentConfig, AgentRun, Email, Mailbox
from .schemas import serialize_email

log = logging.getLogger("mail_agent.pipeline")


def _send_fn_for(provider: str):
    """Which client actually delivers a reply — shared by the auto-send path
    below and api.py's manual "Reply" button, so both make the same choice.
    Graph orgs always send via Graph (HTTP, already unaffected). IMAP orgs send
    via SendGrid's HTTP API instead of raw SMTP when configured, since raw SMTP
    can't succeed on a platform that doesn't route outbound SMTP traffic at all
    (see sendgrid_client.py) — falls back to raw SMTP when SendGrid isn't
    configured, for hosts where SMTP does work (e.g. local dev)."""
    if provider == "graph":
        return graph_client.send_reply
    if settings.sendgrid_api_key:
        return sendgrid_client.send_reply
    return mail_client.send_reply

# In-memory guard against two overlapping runs for the same org — a manual "Run
# now"/"Summarize previous 100" click and a cron tick landing at the same
# moment, or a double-click. Shared by fetch_and_process AND analyze_backlog, so
# the two can never race on the same org's rows. Only valid because this service
# runs as a single process (see run-all.ps1 — no --workers); the scheduler's own
# tick-pileup guard defers to this same set.
_running: set[str] = set()

# A row stuck at summary_status="pending" past this age was picked up by an
# analysis pass that never finished it (crashed process, killed connection) —
# eligible for pickup again, same as a freshly-"skipped" row (see
# `_select_for_analysis`), rather than staying invisible forever.
_STUCK_PENDING_AFTER = timedelta(minutes=10)

# How many messages one fetch round pulls from the mailbox at a time. Small
# enough that a single batch's IMAP/Graph round trip stays fast and its progress
# commits to the DB promptly (visible to anyone polling GET /agent/status); the
# loop below just runs more rounds for anything beyond it.
_BATCH_SIZE = 100

# Safety ceiling on how many *new* emails a single run will import, even when
# the caller asked for everything (count=0). A mailbox with a bigger backlog
# than this simply continues on the next run — the sync watermark (see
# Mailbox.last_synced_uid/at) has already advanced past everything this run did
# handle, so nothing is lost or re-done. This is what keeps "the first sync on a
# mailbox with tens of thousands of emails" graceful: bounded, visible-progress
# batches across as many runs as it takes, never one multi-hour request.
_MAX_PER_RUN = 2000

# How many of the most-recently-received not-yet-analyzed emails a sync
# automatically summarizes, so the inbox is immediately useful without forcing
# an AI call on the entire imported backlog. Anything beyond this stays
# "skipped" until a manual catch-up (Settings' "Summarize previous 100", or a
# per-email retry) asks for more.
_AUTO_ANALYZE_COUNT = 50

# Default batch size for a manual backlog catch-up (see analyze_backlog).
_BACKLOG_ANALYZE_DEFAULT = 100

_ANALYZE_CONCURRENCY = 5


def is_running(org_id: str) -> bool:
    return org_id in _running


async def load_send_context(org_id: str) -> tuple[dict, str, bool] | None:
    """Send creds (shape depends on provider — Graph reuses its read creds,
    IMAP orgs get SMTP creds) + the org's auto-reply setting — everything an
    analysis pass needs besides the emails themselves. Returns None if no
    mailbox is set up."""
    async with SessionLocal() as s:
        mailbox = await s.scalar(select(Mailbox).where(Mailbox.org_id == org_id))
        if not mailbox:
            return None
        if mailbox.provider == "graph":
            send_creds = {
                "tenant_id": mailbox.tenant_id,
                "client_id": mailbox.client_id,
                "client_secret": crypto.decrypt(mailbox.client_secret_enc),
                "mailbox": mailbox.username,
            }
        else:
            send_creds = {
                "smtp_host": mailbox.smtp_host,
                "smtp_port": mailbox.smtp_port,
                "username": mailbox.username,
                "password": crypto.decrypt(mailbox.password_enc),
            }
        cfg = await s.scalar(select(AgentConfig).where(AgentConfig.org_id == org_id))
        return send_creds, mailbox.provider, bool(cfg and cfg.auto_reply_enabled)


async def _select_for_analysis(org_id: str, limit: int) -> list[tuple[str, dict]]:
    """Picks the most recent `limit` emails eligible for analysis — freshly
    "skipped" ones first in effect, since ordering is by received date, but a
    stuck "pending" row from an interrupted earlier pass is just as eligible
    (see _STUCK_PENDING_AFTER) rather than sitting invisible forever. Marks
    what it picks as "pending" immediately, so the UI shows "summarizing…" for
    them while the LLM calls are in flight."""
    if limit <= 0:
        return []
    stale_cutoff = datetime.now(timezone.utc) - _STUCK_PENDING_AFTER
    async with SessionLocal() as s:
        rows = list(await s.scalars(
            select(Email)
            .where(
                Email.org_id == org_id,
                or_(
                    Email.summary_status == "skipped",
                    and_(Email.summary_status == "pending", Email.created_at < stale_cutoff),
                ),
            )
            .order_by(Email.received_at.desc())
            .limit(limit)
        ))
        for e in rows:
            e.summary_status = "pending"
        await s.commit()
        return [(e.id, serialize_email(e)) for e in rows]


async def _count_skipped(org_id: str) -> int:
    async with SessionLocal() as s:
        rows = await s.scalars(select(Email.id).where(Email.org_id == org_id, Email.summary_status == "skipped"))
        return len(list(rows))


async def analyze_and_persist(
    org_id: str, rows: list[tuple[str, dict]], *, auto_reply_enabled: bool, send_creds: dict, provider: str,
    sem: asyncio.Semaphore | None = None, attachments_by_id: dict[str, list[dict]] | None = None,
) -> tuple[int, int, bool]:
    """Analyzes + persists a batch of (email_id, payload) pairs — the shared
    core used by a sync's post-fetch auto-analysis, a manual backlog catch-up,
    and the per-email retry button. Returns (analyzed_ok_count, high_priority_count, all_ok).

    `attachments_by_id`, when given, forwards each email's attachment bytes (never
    persisted to the row — see mail_client/graph_client) to the CRM alongside any
    referral/case-communication/reschedule detected in it. Only the initial sync's
    caller has these on hand — a backlog catch-up or a later retry has no way to
    recover attachment bytes for an already-stored row, so those calls omit it and
    a detected intent still files to the CRM, just without attachments."""
    if not rows:
        return 0, 0, True
    sem = sem or asyncio.Semaphore(_ANALYZE_CONCURRENCY)

    async def analyze(email_id: str, payload: dict) -> tuple[str, dict, bool, bool]:
        async with sem:
            try:
                res = await agent_client.analyze_email(payload)
                a = res.get("analysis") or {}
                log.info("analyzed uid=%s -> %s/%s", payload.get("uid"), a.get("category"), a.get("priority"))
                return email_id, a, bool(res.get("auto_sendable")), True
            except Exception as exc:
                log.warning("analysis FAILED uid=%s: %s", payload.get("uid"), exc)
                return email_id, {}, False, False

    results = await asyncio.gather(*(analyze(eid, p) for eid, p in rows))

    # Auto-send eligible replies via Graph or SMTP (matching the mailbox's read
    # provider) — NO DB connection held, same principle as the analyze step (a
    # slow network call must never sit inside an open transaction). `rows`'
    # serialized payloads already carry fromEmail/subject, so this needs no
    # extra DB read.
    payload_by_id = {eid: p for eid, p in rows}
    send_fn = _send_fn_for(provider)

    async def maybe_send(email_id: str, a: dict, auto_sendable: bool, ok: bool) -> tuple[str, dict, bool, bool]:
        if not (ok and auto_reply_enabled and auto_sendable and a.get("needs_reply")):
            return email_id, a, ok, False
        payload = payload_by_id[email_id]
        async with sem:
            try:
                await run_in_threadpool(
                    send_fn,
                    **send_creds, to_addr=payload["fromEmail"],
                    subject=f"Re: {payload['subject']}", body=a.get("suggested_reply", "") or "",
                )
                return email_id, a, ok, True
            except Exception as exc:
                log.warning("auto-send FAILED uid=%s: %s — falling back to draft", payload.get("uid"), exc)
                return email_id, a, ok, False

    sent_results = await asyncio.gather(*(maybe_send(eid, a, auto_sendable, ok) for eid, a, auto_sendable, ok in results))

    # CRM forwarding — also NO DB connection held, same principle as analyze/
    # maybe_send above. A referral/communication submission (plus any attachment
    # upload ahead of it) is an HTTP call that can take seconds; running it here
    # rather than inside the persist transaction below means a slow or failing
    # CRM call can never hold a pooled connection open or roll back rows that
    # already finished processing.
    async def do_crm(email_id: str, a: dict, ok: bool) -> tuple[str, str, str | None, str | None]:
        if not ok:
            return email_id, "none", None, None
        async with sem:
            status, ref, activity_ref = await crm_sync.after_email_analysis(
                payload_by_id[email_id], a, (attachments_by_id or {}).get(email_id)
            )
            return email_id, status, ref, activity_ref

    crm_results = await asyncio.gather(*(do_crm(eid, a, ok) for eid, a, ok, _sent in sent_results))
    crm_by_id = {eid: (status, ref, activity_ref) for eid, status, ref, activity_ref in crm_results}

    high = 0
    async with SessionLocal() as s:
        for email_id, a, ok, sent in sent_results:
            e = await s.get(Email, email_id)
            if not e:
                continue
            if ok:
                e.summary = a.get("summary", "")
                e.category = a.get("category", "")
                e.priority = a.get("priority", "Medium")
                e.confidence = a.get("confidence", 0.0)
                e.needs_reply = bool(a.get("needs_reply", False))
                e.summary_status = "done"
                if e.priority == "High":
                    high += 1
                e.intent = a.get("intent") or "NONE"
                e.crm_status, e.crm_reference, e.activity_ref = crm_by_id.get(email_id, ("none", None, None))

                suggested_reply = a.get("suggested_reply", "") or ""
                if not e.needs_reply:
                    pass  # reply_status stays "none"
                elif sent:
                    e.draft_reply = suggested_reply
                    e.reply_status = "sent"
                    e.auto_sent = True
                else:
                    # Either not auto-sendable (needs a human), or the send
                    # attempt itself failed — either way, land it as a draft
                    # rather than losing the AI's suggested reply.
                    e.draft_reply = suggested_reply
                    e.reply_status = "draft"
            else:
                e.summary_status = "failed"
        await s.commit()

    ok_count = sum(1 for _, _, ok, _ in sent_results if ok)
    all_ok = all(ok for _, _, ok, _ in sent_results)
    return ok_count, high, all_ok


async def analyze_backlog(org_id: str, count: int = _BACKLOG_ANALYZE_DEFAULT) -> dict:
    """Manually analyzes the next `count` not-yet-analyzed emails (most recent
    first among what's still "skipped") — Settings' "Summarize previous 100",
    repeatable to work through a backlog at the org's own pace/cost, and usable
    right away since it runs synchronously (bounded, unlike a full sync).

    Raises RuntimeError if a sync or another analyze pass is already running for
    this org, LookupError if no mailbox is configured yet."""
    if org_id in _running:
        raise RuntimeError(f"a sync/analyze is already in progress for org={org_id}")
    _running.add(org_id)
    try:
        ctx = await load_send_context(org_id)
        if ctx is None:
            raise LookupError("no mailbox configured for this org")
        send_creds, provider, auto_reply_enabled = ctx

        rows = await _select_for_analysis(org_id, count)
        ok, high, all_ok = await analyze_and_persist(
            org_id, rows, auto_reply_enabled=auto_reply_enabled, send_creds=send_creds, provider=provider,
        )
        remaining = await _count_skipped(org_id)
        log.info("analyze_backlog: org=%s analyzed=%d high=%d remaining=%d all_ok=%s", org_id, ok, high, remaining, all_ok)
        return {"analyzed": ok, "highPriority": high, "remaining": remaining, "allOk": all_ok}
    finally:
        _running.discard(org_id)


async def fetch_and_process(
    org_id: str, count: int = 0, *, sweep: bool = True, reset_watermark: bool = False,
) -> list[str]:
    """Returns the ids of newly-fetched emails.

    `count=0` (the default) means "everything not yet synced" — bounded per run
    only by `_MAX_PER_RUN` for safety, not by a fixed per-call cap. Pass a
    positive `count` to cap a single call explicitly.

    `reset_watermark=True` ignores the mailbox's stored sync watermark for this
    run, as if it had never been synced before — a full mailbox rescan (still
    deduped against what's already stored, so nothing is inserted twice), for
    recovery/audit use rather than normal operation.

    Only the most recent `_AUTO_ANALYZE_COUNT` imported-but-unanalyzed emails
    get an AI summary automatically — see this module's docstring for why.

    Raises RuntimeError if a run for this org is already in progress.

    Runs as a background task (see api.py's /emails/fetch) rather than being
    awaited inline by an HTTP request — a full sync can take minutes (or, on a
    large backlog, span several runs), far longer than any request should stay
    open. An `AgentRun` row is created up front with status="running" and its
    progress counters update after every batch, so any page can poll GET
    /agent/status to see live progress, regardless of which page issued the
    request or whether that page is even still mounted."""
    if org_id in _running:
        raise RuntimeError(f"a sync is already in progress for org={org_id}")
    _running.add(org_id)

    async with SessionLocal() as s:
        run = AgentRun(org_id=org_id, status="running")
        s.add(run)
        await s.commit()
        run_id = run.id
    log.info("fetch start: org=%s count=%d sweep=%s reset_watermark=%s run=%s", org_id, count, sweep, reset_watermark, run_id)

    try:
        return await _run(org_id, count, sweep, reset_watermark, run_id)
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


async def _known_uids(org_id: str, uid_validity: str | None) -> set[str]:
    """UIDs already stored for this org, scoped to the *current* UID epoch
    (`uid_validity`) plus any legacy rows that pre-date the uid_validity column
    (NULL — those never had an epoch recorded, so they're included unconditionally
    as a conservative belt-and-suspenders: excluding them could only ever risk a
    duplicate insert of already-known mail, never a silent loss). Scoping by
    epoch is what lets a genuinely new message reuse a UID number a *prior* IMAP
    UIDVALIDITY era already used without being wrongly treated as a duplicate —
    see models.py's Email.uid_validity."""
    async with SessionLocal() as s:
        return set(await s.scalars(
            select(Email.uid).where(
                Email.org_id == org_id,
                or_(Email.uid_validity == uid_validity, Email.uid_validity.is_(None)),
            )
        ))


async def _run(org_id: str, count: int, sweep: bool, reset_watermark: bool, run_id: str) -> list[str]:
    # 1) short txn: read mailbox creds + org config + the sync watermark
    async with SessionLocal() as s:
        mailbox = await s.scalar(select(Mailbox).where(Mailbox.org_id == org_id))
        if not mailbox or not mailbox.enabled:
            log.warning("fetch aborted: org=%s has no enabled mailbox", org_id)
            raise LookupError("no mailbox configured for this org")
        mailbox_id = mailbox.id
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
        # Send creds for the possible auto-send branch below — read once here so
        # a high-confidence, non-sensitive reply can go out without a second query.
        # Graph mode sends through the same app registration it reads with, so
        # this just reuses `creds` rather than decrypting a second secret.
        send_creds = creds if provider == "graph" else {
            "smtp_host": mailbox.smtp_host,
            "smtp_port": mailbox.smtp_port,
            "username": mailbox.username,
            "password": crypto.decrypt(mailbox.password_enc),
        }
        cfg = await s.scalar(select(AgentConfig).where(AgentConfig.org_id == org_id))
        auto_reply_enabled = bool(cfg and cfg.auto_reply_enabled)

        since_uid = None if reset_watermark else mailbox.last_synced_uid
        since_at = None if reset_watermark else mailbox.last_synced_at
        # NULL on both means this mailbox has never been synced (or a rescan was
        # requested) — nothing to narrow by, so the first batch below naturally
        # pulls from the very start of the mailbox's history.

        # The UID epoch dedup is currently scoped to (see _known_uids/Email.uid_validity).
        # Graph ids need no epoch tracking — "graph" is a fixed sentinel, never reset.
        # For IMAP, None means no baseline established yet (first sync since this
        # column was added, or ever); the batch loop below adopts whatever the
        # server reports as the baseline the first time it sees one.
        current_uid_validity = "graph" if provider == "graph" else mailbox.uid_validity

    sem = asyncio.Semaphore(_ANALYZE_CONCURRENCY)
    all_new_ids: list[str] = []
    total_fetched = total_processed = total_archived = 0
    swept = False
    batch_num = 0
    # Attachment bytes never persist to a row (see mail_client/graph_client) — held
    # here only long enough to reach analyze_and_persist's CRM-forwarding step below,
    # keyed by the email id assigned at insert time.
    attachments_by_id: dict[str, list[dict]] = {}

    # --- import phase: fetch + dedup + insert(skipped) + sweep + advance watermark ---
    while True:
        batch_num += 1
        batch_cap = min(count, _BATCH_SIZE) if count else _BATCH_SIZE
        known_uids = await _known_uids(org_id, current_uid_validity)

        if provider == "graph":
            messages, watermark_advance = await run_in_threadpool(
                graph_client.fetch_latest, count=batch_cap, known_uids=known_uids, since_at=since_at, **creds
            )
        else:
            messages, watermark_advance, fetched_uid_validity = await run_in_threadpool(
                mail_client.fetch_latest, count=batch_cap, known_uids=known_uids, since_uid=since_uid, **creds
            )
            if current_uid_validity is not None and fetched_uid_validity and fetched_uid_validity != current_uid_validity:
                # A real IMAP UIDVALIDITY reset (mailbox rebuild/migration on the
                # server) — UID numbers started over, so `since_uid` (scoped to the
                # *old* numbering) is now meaningless, and this batch's `messages`
                # were fetched using it, so they can't be trusted either. Adopt the
                # new epoch, persist it + clear the watermark immediately (so a
                # crash right after this doesn't lose the update), and restart the
                # loop to refetch cleanly from the start of the new epoch.
                log.warning(
                    "org=%s: IMAP UIDVALIDITY changed (%s -> %s) — mailbox UID numbering was "
                    "reset server-side; resetting the sync watermark and starting a fresh UID "
                    "epoch so a reused UID is never mistaken for an already-imported message",
                    org_id, current_uid_validity, fetched_uid_validity,
                )
                current_uid_validity = fetched_uid_validity
                since_uid = None
                async with SessionLocal() as s:
                    mb = await s.get(Mailbox, mailbox_id)
                    if mb:
                        mb.uid_validity = current_uid_validity
                        mb.last_synced_uid = None
                    await s.commit()
                continue
            if fetched_uid_validity and current_uid_validity is None:
                current_uid_validity = fetched_uid_validity  # first baseline for this mailbox
        log.info("org=%s batch=%d: pulled %d message(s)", org_id, batch_num, len(messages))
        if not messages:
            break

        new: list[str] = []
        async with SessionLocal() as s:
            # Dedup against the same `known_uids` already fetched once above (and
            # already handed to the provider to skip) instead of a per-message
            # SELECT round trip — this loop used to issue one query per message
            # in the batch for what `known_uids` already tells us in memory.
            fresh = [m for m in messages if m["uid"] not in known_uids]

            if sweep and fresh and not swept:
                now = datetime.now(timezone.utc)
                # A bulk UPDATE, not a SELECT-then-mutate — the earlier ORM-load
                # form (`select(Email)...`) pulled every prior email's full body/
                # summary/draft_reply text over the wire just to flip two flags,
                # which is what was blowing through Neon's data-transfer quota on
                # every sweep. This flips the flags in the DB directly.
                result = await s.execute(
                    sa_update(Email)
                    .where(Email.org_id == org_id, Email.archived_at.is_(None))
                    .values(archived_at=now, archived_by="cron")
                )
                total_archived = max(result.rowcount, 0)
                swept = True
                if total_archived:
                    log.info("sweep: archived %d prior email(s)", total_archived)

            for m in fresh:
                content_type = m.get("contentType", "text")
                body = m["body"]
                if content_type == "html":
                    # Server-side sanitization, ahead of the frontend's sandboxed
                    # iframe — defense in depth against a malicious sender's HTML.
                    body = nh3.clean(body)
                row = Email(
                    org_id=org_id, uid=m["uid"], uid_validity=current_uid_validity,
                    sender=m["from"], from_email=m["fromEmail"],
                    subject=m["subject"], body=body, content_type=content_type,
                    received_at=m["receivedAt"], summary_status="skipped",
                )
                s.add(row)
                await s.flush()
                new.append(row.id)
                attachments_by_id[row.id] = m.get("attachments") or []

            # Advance the watermark to what this batch *examined* (not just what
            # ended up fresh) — a transient single-message fetch failure or an
            # already-known message still counts as "looked at", so the next
            # batch/run doesn't re-scan it. Never regresses.
            mb = await s.get(Mailbox, mailbox_id)
            if mb and provider == "graph" and watermark_advance:
                if not mb.last_synced_at or watermark_advance > mb.last_synced_at:
                    mb.last_synced_at = watermark_advance
                since_at = mb.last_synced_at
            elif mb and watermark_advance:
                if not mb.last_synced_uid or int(watermark_advance) > int(mb.last_synced_uid):
                    mb.last_synced_uid = watermark_advance
                since_uid = mb.last_synced_uid
                if current_uid_validity:
                    mb.uid_validity = current_uid_validity

            run = await s.get(AgentRun, run_id)
            if run:
                run.fetched += len(messages)
                run.processed += len(new)
                run.archived = total_archived
            await s.commit()
        log.info("org=%s batch=%d: %d new email(s) after dedup", org_id, batch_num, len(new))

        total_fetched += len(messages)
        total_processed += len(new)
        all_new_ids.extend(new)

        if count and total_processed >= count:
            break
        if total_processed >= _MAX_PER_RUN:
            log.info(
                "org=%s hit the per-run import ceiling (%d) — remaining backlog continues on the next run",
                org_id, _MAX_PER_RUN,
            )
            break
        if len(messages) < batch_cap:
            break  # the mailbox had fewer than a full batch left — caught up

    # --- analysis phase: summarize only the most recent slice, once, after import ---
    rows = await _select_for_analysis(org_id, _AUTO_ANALYZE_COUNT)
    _, high, all_ok = await analyze_and_persist(
        org_id, rows, auto_reply_enabled=auto_reply_enabled, send_creds=send_creds, provider=provider, sem=sem,
        attachments_by_id=attachments_by_id,
    )

    async with SessionLocal() as s:
        run = await s.get(AgentRun, run_id)
        if run:
            run.high_priority = high
            run.status = "success" if all_ok else "partial"
        await s.commit()
    log.info(
        "run recorded: org=%s fetched=%d imported=%d analyzed=%d high=%d archived=%d run=%s",
        org_id, total_fetched, total_processed, len(rows), high, total_archived, run_id,
    )

    return all_new_ids
