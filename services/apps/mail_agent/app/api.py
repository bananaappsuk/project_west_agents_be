from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import billing_client, crypto, graph_client, mail_client, pipeline
from .billing_client import BillingBlocked
from .config import settings
from .db import get_session
from .deps import require
from .models import AgentConfig, AgentRun, Email, Folder, Mailbox
from .scheduler import _interval_minutes
from .schemas import (
    AgentConfigIn,
    FolderIn,
    FolderPatch,
    IdsIn,
    MailboxIn,
    MoveIn,
    ReplyIn,
    serialize_email,
    serialize_folder,
)

router = APIRouter()

READ = f"{settings.app_key}:emails.read"
WRITE = f"{settings.app_key}:emails.write"


def _org(claims: dict) -> str:
    return claims["org"]


async def _get_email(session: AsyncSession, org: str, email_id: str) -> Email:
    e = await session.scalar(select(Email).where(Email.org_id == org, Email.id == email_id))
    if not e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "email not found")
    return e


# ---------- health ----------
@router.get("/health")
async def health():
    return {"status": "ok", "service": "mail_agent"}


# ---------- emails ----------
@router.get("/emails")
async def list_emails(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    rows = await session.scalars(
        select(Email).where(Email.org_id == _org(claims)).order_by(Email.received_at.desc())
    )
    return [serialize_email(e) for e in rows]


@router.post("/emails/fetch", status_code=status.HTTP_202_ACCEPTED)
async def fetch_emails(
    background_tasks: BackgroundTasks,
    count: int = 0,
    full_resync: bool = False,
    claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)
):
    """Kicks off a fetch+analyze run in the background and returns immediately —
    a full sync can take minutes on a large mailbox (sequential LLM calls, 5 at a
    time), far longer than any request should stay open. The billing/mailbox
    checks below stay synchronous so the common failure cases still surface
    immediately; the slow work is what moved to the background. Poll GET
    /agent/status (its `running` flag, and each history entry's live progress
    counters) to know how it's going — that's real server state, so it stays
    correct no matter which page you're on.

    `count=0` (the default) fetches *everything* not yet synced — a mailbox's
    first-ever sync naturally gets its whole history this way, and every sync
    after that only ever asks for what's actually new (see pipeline.py's sync
    watermark), not a fixed per-run cap. `full_resync=True` ignores the stored
    watermark and rescans the whole mailbox from the start — a recovery/audit
    tool, not something a normal sync needs."""
    org = _org(claims)
    try:
        await billing_client.check_entitlement(org)
    except BillingBlocked as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, {"code": exc.code, "message": str(exc)}) from exc

    mailbox = await session.scalar(select(Mailbox).where(Mailbox.org_id == org))
    if not mailbox or not mailbox.enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no mailbox configured for this org")

    if pipeline.is_running(org):
        raise HTTPException(status.HTTP_409_CONFLICT, "a sync is already in progress")

    background_tasks.add_task(pipeline.fetch_and_process, org, count, sweep=True, reset_watermark=full_resync)
    return {"status": "started"}


@router.post("/emails/move", status_code=status.HTTP_204_NO_CONTENT)
async def move_to_folder(body: MoveIn, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    rows = await session.scalars(select(Email).where(Email.org_id == _org(claims), Email.id.in_(body.ids)))
    for e in rows:
        e.folder_id = body.folderId
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/emails/archive", status_code=status.HTTP_204_NO_CONTENT)
async def archive_emails(body: IdsIn, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    now = datetime.now(timezone.utc)
    rows = await session.scalars(select(Email).where(Email.org_id == _org(claims), Email.id.in_(body.ids)))
    for e in rows:
        e.archived_at = now
        e.archived_by = "user"
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/emails/unarchive", status_code=status.HTTP_204_NO_CONTENT)
async def unarchive_emails(body: IdsIn, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    rows = await session.scalars(select(Email).where(Email.org_id == _org(claims), Email.id.in_(body.ids)))
    for e in rows:
        e.archived_at = None
        e.archived_by = None
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/emails/auto-reply", status_code=status.HTTP_204_NO_CONTENT)
async def generate_drafts(body: IdsIn, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    """Manual "generate a draft" action. Most emails already get an AI-drafted
    reply at analysis time (see pipeline.py) — this just surfaces it as a draft.
    Rows analyzed before that shipped have no draft_reply yet, so those fall back
    to a fixed template rather than leaving the button a no-op."""
    rows = list(await session.scalars(select(Email).where(Email.org_id == _org(claims), Email.id.in_(body.ids))))
    for e in rows:
        if not e.draft_reply:
            first = (e.sender or "there").split()[0]
            e.draft_reply = (
                f"Hi {first},\n\nThanks for reaching out regarding \"{e.subject}\". "
                "We've received your message and will get back to you shortly.\n\n"
                "Best regards,\nSupport Team"
            )
        e.reply_status = "draft"
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/emails/{email_id}/reply/dismiss", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_reply(email_id: str, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    """Human-in-the-loop reject: the AI's draft isn't wanted. The draft stays on
    the row (for audit) but reply_status flips so it drops out of the review queue."""
    e = await _get_email(session, _org(claims), email_id)
    e.reply_status = "rejected"
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/emails/{email_id}/reply", status_code=status.HTTP_204_NO_CONTENT)
async def send_reply(email_id: str, body: ReplyIn, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    e = await _get_email(session, _org(claims), email_id)
    mailbox = await session.scalar(select(Mailbox).where(Mailbox.org_id == _org(claims)))
    if not mailbox:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no mailbox configured")
    try:
        if mailbox.provider == "graph":
            await run_in_threadpool(
                graph_client.send_reply,
                tenant_id=mailbox.tenant_id,
                client_id=mailbox.client_id,
                client_secret=crypto.decrypt(mailbox.client_secret_enc),
                mailbox=mailbox.username,
                to_addr=e.from_email,
                subject=f"Re: {e.subject}",
                body=body.body,
            )
        else:
            await run_in_threadpool(
                mail_client.send_reply,
                smtp_host=mailbox.smtp_host,
                smtp_port=mailbox.smtp_port,
                username=mailbox.username,
                password=crypto.decrypt(mailbox.password_enc),
                to_addr=e.from_email,
                subject=f"Re: {e.subject}",
                body=body.body,
            )
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"send failed: {exc}") from exc
    e.reply_status = "sent"
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/emails/{email_id}/retry-summary")
async def retry_summary(email_id: str, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    """(Re-)analyzes exactly this one email — the per-email "AI-summarize"
    action, for a "skipped" (never analyzed — see pipeline.py) or "failed" row
    alike. Goes through the same shared analyze_and_persist pipeline.py uses
    everywhere else, so this gets the full result (confidence, needs_reply,
    auto-reply draft, CRM sync, ...), not just summary/category/priority."""
    org = _org(claims)
    e = await _get_email(session, org, email_id)
    e.summary_status = "pending"
    payload = serialize_email(e)
    await session.commit()

    ctx = await pipeline.load_send_context(org)
    if ctx is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no mailbox configured for this org")
    send_creds, provider, auto_reply_enabled = ctx
    await pipeline.analyze_and_persist(
        org, [(email_id, payload)], auto_reply_enabled=auto_reply_enabled, send_creds=send_creds, provider=provider,
    )

    await session.refresh(e)
    return serialize_email(e)


@router.post("/emails/analyze-backlog")
async def analyze_backlog(
    count: int = 100, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)
):
    """Settings' "Summarize previous 100" — manually analyzes the next `count`
    not-yet-analyzed emails (see pipeline.py: a sync only auto-analyzes the most
    recent slice, so older imported mail sits "skipped" until asked for).
    Bounded and synchronous (not backgrounded) — a 100-email batch finishes in
    well under the frontend's request timeout, so the button can just show its
    own "summarizing…" state and get a direct result back, no polling needed."""
    org = _org(claims)
    try:
        result = await pipeline.analyze_backlog(org, count)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return result


# ---------- folders ----------
@router.get("/folders")
async def list_folders(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    rows = await session.scalars(select(Folder).where(Folder.org_id == _org(claims)).order_by(Folder.created_at))
    return [serialize_folder(f) for f in rows]


@router.post("/folders", status_code=status.HTTP_201_CREATED)
async def create_folder(body: FolderIn, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    folder = Folder(org_id=_org(claims), name=body.name, color=body.color)
    session.add(folder)
    await session.commit()
    return serialize_folder(folder)


@router.put("/folders/{folder_id}")
async def update_folder(folder_id: str, body: FolderPatch, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    f = await session.scalar(select(Folder).where(Folder.org_id == _org(claims), Folder.id == folder_id))
    if not f:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "folder not found")
    if body.name is not None:
        f.name = body.name
    if body.color is not None:
        f.color = body.color
    await session.commit()
    return serialize_folder(f)


@router.delete("/folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_folder(folder_id: str, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    # Contents move to All (folder_id = null) — never cascade-delete the emails.
    emails = await session.scalars(select(Email).where(Email.org_id == _org(claims), Email.folder_id == folder_id))
    for e in emails:
        e.folder_id = None
    f = await session.scalar(select(Folder).where(Folder.org_id == _org(claims), Folder.id == folder_id))
    if f:
        await session.delete(f)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- alerts ----------
@router.get("/alerts")
async def list_alerts(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    rows = await session.scalars(select(Email).where(Email.org_id == _org(claims)))
    alerts = []
    for e in rows:
        is_security = e.category == "Security"
        if e.priority == "High" or is_security:
            alerts.append({
                "id": f"a-{e.id}",
                "emailId": e.id,
                "type": "classification" if is_security else "priority",
                "message": (f'Security email flagged: "{e.subject}"' if is_security
                            else f'High-priority: "{e.subject}"'),
                "priority": e.priority,
                "createdAt": e.received_at.isoformat() if e.received_at else None,
                "archived": e.archived_at is not None,
            })
    return alerts


# ---------- agent status ----------
async def _get_config(session: AsyncSession, org: str) -> AgentConfig:
    cfg = await session.scalar(select(AgentConfig).where(AgentConfig.org_id == org))
    if not cfg:
        cfg = AgentConfig(org_id=org, fetch_per_run=settings.fetch_per_run, enabled=settings.cron_enabled)
        session.add(cfg)
        await session.commit()
    return cfg


@router.get("/agent/status")
async def agent_status(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    org = _org(claims)
    cfg = await _get_config(session, org)
    runs = list(await session.scalars(select(AgentRun).where(AgentRun.org_id == org).order_by(AgentRun.run_at.desc())))
    last = runs[0] if runs else None

    daily: dict[str, dict] = {}
    for r in runs:
        day = r.run_at.date().isoformat()
        d = daily.setdefault(day, {"day": day, "processed": 0, "highPriority": 0})
        d["processed"] += r.processed
        d["highPriority"] += r.high_priority

    next_run = ""
    if cfg.enabled and last:
        next_run = (last.run_at + timedelta(minutes=_interval_minutes(cfg.cron_interval))).isoformat()

    return {
        "running": pipeline.is_running(org),
        "config": {
            "cronInterval": cfg.cron_interval, "fetchPerRun": cfg.fetch_per_run, "enabled": cfg.enabled,
            "autoReplyEnabled": cfg.auto_reply_enabled,
        },
        "stats": {
            "totalRuns": len(runs),
            "lastRunAt": last.run_at.isoformat() if last else "",
            "nextRunAt": next_run,
            "lastFetchCount": last.fetched if last else 0,
            "totalFetched": sum(r.fetched for r in runs),
            # "Messages processed" (see KpiCard on the dashboard) means genuinely new,
            # deduped mail actually summarized — `fetched` is the raw per-run IMAP/Graph
            # scan count and can legitimately re-scan the same mailbox window run after
            # run without anything new arriving, so it isn't the right number to show.
            "totalProcessed": sum(r.processed for r in runs),
            "totalHighPriority": sum(r.high_priority for r in runs),
        },
        "history": [
            {"runAt": r.run_at.isoformat(), "fetched": r.fetched, "processed": r.processed,
             "highPriority": r.high_priority, "archived": r.archived,
             "status": r.status, "errorMessage": r.error_message}
            for r in runs[:5]
        ],
        "daily": sorted(daily.values(), key=lambda d: d["day"])[-7:],
    }


@router.put("/settings/agent")
async def update_agent_config(body: AgentConfigIn, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    cfg = await _get_config(session, _org(claims))
    if body.cronInterval is not None:
        cfg.cron_interval = body.cronInterval
    if body.fetchPerRun is not None:
        cfg.fetch_per_run = body.fetchPerRun
    if body.enabled is not None:
        cfg.enabled = body.enabled
    if body.autoReplyEnabled is not None:
        cfg.auto_reply_enabled = body.autoReplyEnabled
    await session.commit()
    return {
        "cronInterval": cfg.cron_interval, "fetchPerRun": cfg.fetch_per_run, "enabled": cfg.enabled,
        "autoReplyEnabled": cfg.auto_reply_enabled,
    }


# ---------- mailbox settings ----------
@router.get("/settings/mailbox")
async def get_mailbox(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    """Non-secret mailbox settings for prefilling the form. Password is never returned."""
    mb = await session.scalar(select(Mailbox).where(Mailbox.org_id == _org(claims)))
    if not mb:
        return None
    return {
        "provider": mb.provider,
        "imapHost": mb.imap_host,
        "imapPort": mb.imap_port,
        "smtpHost": mb.smtp_host,
        "smtpPort": mb.smtp_port,
        "username": mb.username,
        "tenantId": mb.tenant_id,
        "clientId": mb.client_id,
        "clientSecretConfigured": bool(mb.client_secret_enc),
        "enabled": mb.enabled,
        "configured": True,
    }


@router.post("/settings/mailbox")
async def save_mailbox(body: MailboxIn, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    org = _org(claims)
    mailbox = await session.scalar(select(Mailbox).where(Mailbox.org_id == org))
    enc = crypto.encrypt(body.password.replace(" ", ""))  # app passwords are shown spaced; store without
    # clientSecret is write-only-if-changed: a blank value keeps whatever was already stored.
    client_secret_enc = (
        crypto.encrypt(body.clientSecret) if body.clientSecret
        else (mailbox.client_secret_enc if mailbox else None)
    )
    if mailbox:
        mailbox.imap_host, mailbox.imap_port = body.imapHost, body.imapPort
        mailbox.smtp_host, mailbox.smtp_port = body.smtpHost, body.smtpPort
        mailbox.username, mailbox.password_enc, mailbox.enabled = body.username, enc, True
        mailbox.provider = body.provider
        mailbox.tenant_id, mailbox.client_id, mailbox.client_secret_enc = body.tenantId, body.clientId, client_secret_enc
    else:
        session.add(Mailbox(
            org_id=org, imap_host=body.imapHost, imap_port=body.imapPort,
            smtp_host=body.smtpHost, smtp_port=body.smtpPort,
            username=body.username, password_enc=enc, enabled=True,
            provider=body.provider, tenant_id=body.tenantId, client_id=body.clientId,
            client_secret_enc=client_secret_enc,
        ))
    await session.commit()
    return {"ok": True}


@router.post("/settings/mailbox/test")
async def test_mailbox(body: MailboxIn, claims: dict = Depends(require(WRITE))):
    try:
        if body.provider == "graph":
            if not (body.tenantId and body.clientId and body.clientSecret):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "tenantId, clientId and clientSecret are required")
            await run_in_threadpool(
                graph_client.test_connection,
                tenant_id=body.tenantId, client_id=body.clientId, client_secret=body.clientSecret,
                mailbox=body.username,
            )
        else:
            await run_in_threadpool(
                mail_client.test_connection,
                imap_host=body.imapHost, imap_port=body.imapPort,
                username=body.username, password=body.password.replace(" ", ""),
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"connection failed: {exc}") from exc
    return {"ok": True}
