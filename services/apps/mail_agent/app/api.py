from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import agent_client, crypto, mail_client, pipeline
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


@router.post("/emails/fetch")
async def fetch_emails(
    count: int = 20, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)
):
    try:
        new_ids = await pipeline.fetch_and_process(_org(claims), count)
    except LookupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"mailbox fetch failed: {exc}") from exc
    if not new_ids:
        return []
    rows = await session.scalars(
        select(Email).where(Email.id.in_(new_ids)).order_by(Email.received_at.desc())
    )
    return [serialize_email(e) for e in rows]


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
    rows = list(await session.scalars(select(Email).where(Email.org_id == _org(claims), Email.id.in_(body.ids))))
    for e in rows:
        first = (e.sender or "there").split()[0]
        e.draft_reply = (
            f"Hi {first},\n\nThanks for reaching out regarding \"{e.subject}\". "
            "We've received your message and will get back to you shortly.\n\n"
            "Best regards,\nSupport Team"
        )
        e.reply_status = "draft"
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/emails/{email_id}/reply", status_code=status.HTTP_204_NO_CONTENT)
async def send_reply(email_id: str, body: ReplyIn, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    e = await _get_email(session, _org(claims), email_id)
    mailbox = await session.scalar(select(Mailbox).where(Mailbox.org_id == _org(claims)))
    if not mailbox:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no mailbox configured")
    try:
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
    e = await _get_email(session, _org(claims), email_id)
    try:
        result = await agent_client.analyze_email(serialize_email(e))
        a = result.get("analysis") or {}
        e.summary, e.category, e.priority = a.get("summary", ""), a.get("category", ""), a.get("priority", "Medium")
        e.summary_status = "done"
    except Exception:
        e.summary_status = "failed"
    await session.commit()
    return serialize_email(e)


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
        "config": {"cronInterval": cfg.cron_interval, "fetchPerRun": cfg.fetch_per_run, "enabled": cfg.enabled},
        "stats": {
            "totalRuns": len(runs),
            "lastRunAt": last.run_at.isoformat() if last else "",
            "nextRunAt": next_run,
            "lastFetchCount": last.fetched if last else 0,
            "totalFetched": sum(r.fetched for r in runs),
            "totalHighPriority": sum(r.high_priority for r in runs),
        },
        "history": [
            {"runAt": r.run_at.isoformat(), "fetched": r.fetched, "processed": r.processed,
             "highPriority": r.high_priority, "status": r.status}
            for r in runs[:20]
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
    await session.commit()
    return {"cronInterval": cfg.cron_interval, "fetchPerRun": cfg.fetch_per_run, "enabled": cfg.enabled}


# ---------- mailbox settings ----------
@router.get("/settings/mailbox")
async def get_mailbox(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    """Non-secret mailbox settings for prefilling the form. Password is never returned."""
    mb = await session.scalar(select(Mailbox).where(Mailbox.org_id == _org(claims)))
    if not mb:
        return None
    return {
        "imapHost": mb.imap_host,
        "imapPort": mb.imap_port,
        "smtpHost": mb.smtp_host,
        "smtpPort": mb.smtp_port,
        "username": mb.username,
        "enabled": mb.enabled,
        "configured": True,
    }


@router.post("/settings/mailbox")
async def save_mailbox(body: MailboxIn, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    org = _org(claims)
    mailbox = await session.scalar(select(Mailbox).where(Mailbox.org_id == org))
    enc = crypto.encrypt(body.password.replace(" ", ""))  # app passwords are shown spaced; store without
    if mailbox:
        mailbox.imap_host, mailbox.imap_port = body.imapHost, body.imapPort
        mailbox.smtp_host, mailbox.smtp_port = body.smtpHost, body.smtpPort
        mailbox.username, mailbox.password_enc, mailbox.enabled = body.username, enc, True
    else:
        session.add(Mailbox(
            org_id=org, imap_host=body.imapHost, imap_port=body.imapPort,
            smtp_host=body.smtpHost, smtp_port=body.smtpPort,
            username=body.username, password_enc=enc, enabled=True,
        ))
    await session.commit()
    return {"ok": True}


@router.post("/settings/mailbox/test")
async def test_mailbox(body: MailboxIn, claims: dict = Depends(require(WRITE))):
    try:
        await run_in_threadpool(
            mail_client.test_connection,
            imap_host=body.imapHost, imap_port=body.imapPort,
            username=body.username, password=body.password.replace(" ", ""),
        )
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"connection failed: {exc}") from exc
    return {"ok": True}
