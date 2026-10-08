from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import crypto, graph_client, mail_client, pipeline
from .config import settings
from .db import get_session
from .deps import mailbox_scope, require
from .mailbox_scope import MailboxScope
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
    serialize_mailbox,
)

router = APIRouter()

READ = f"{settings.app_key}:emails.read"
WRITE = f"{settings.app_key}:emails.write"


async def _get_email(session: AsyncSession, email_id: str, scope: MailboxScope) -> Email:
    """Fetch one email, scoped to the mailboxes this user may read.

    The mailbox filter is part of the query rather than a check applied to the
    result. An out-of-scope row therefore comes back as "not found" -- the same 404
    a non-existent id produces, deliberately. A 403 would confirm the email exists
    and that somebody else can see it, and for a system holding safeguarding
    material that inference is itself a leak.

    Restricted rows are excluded unless the user holds safeguarding clearance, for
    the same reason: absent, not redacted.
    """
    stmt = select(Email).where(
        Email.id == email_id,
        Email.mailbox_id.in_(scope.readable),
    )
    if not scope.safeguarding:
        stmt = stmt.where(Email.restricted.is_(False))
    e = await session.scalar(stmt)
    if not e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "email not found")
    return e


async def _scoped_emails(session: AsyncSession, ids: list[str], scope: MailboxScope):
    """The subset of `ids` this user may actually act on.

    A bulk action takes ids straight from the client, so an unfiltered
    `Email.id.in_(ids)` lets anyone act on any email in the system just by knowing
    its id. Out-of-scope ids are silently skipped rather than rejected: refusing the
    whole batch would tell the caller which ids exist in mailboxes they cannot see,
    and acting on them would be worse still.
    """
    stmt = select(Email).where(
        Email.id.in_(ids),
        Email.mailbox_id.in_(scope.readable),
    )
    if not scope.safeguarding:
        stmt = stmt.where(Email.restricted.is_(False))
    return list(await session.scalars(stmt))


# ---------- health ----------
@router.get("/health")
async def health():
    return {"status": "ok", "service": "mail_agent"}


# ---------- mailboxes ----------
@router.get("/mailboxes")
async def list_mailboxes(
    claims: dict = Depends(require(READ)),
    scope: MailboxScope = Depends(mailbox_scope),
    session: AsyncSession = Depends(get_session),
):
    """The shared mailboxes this user may act on, with their rights on each.

    Returned per user rather than as a fixed list: two people signing in see
    different sets, and the UI must not offer a mailbox the API would then refuse.
    Legacy and invisible mailboxes never appear -- pre-PW POC data is not part of
    the Project West workspace.
    """
    if scope.is_empty:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "you are not authorised for any mailbox - ask IT to add you to the "
            "relevant PW-Mailbox group",
        )
    rows = (await session.execute(
        select(Mailbox)
        .where(Mailbox.id.in_(scope.readable), Mailbox.visible.is_(True))
        .order_by(Mailbox.display_name)
    )).scalars().all()
    return [
        serialize_mailbox(
            m,
            can_read=True,
            can_send=m.id in scope.sendable,
            can_approve=m.id in scope.approvable,
        )
        for m in rows
    ]


# ---------- emails ----------
@router.get("/emails")
async def list_emails(
    mailboxId: str | None = None,
    claims: dict = Depends(require(READ)),
    scope: MailboxScope = Depends(mailbox_scope),
    session: AsyncSession = Depends(get_session),
):
    if scope.is_empty:
        # Explicit, not an empty list. A user authorised for nothing must see an
        # error they can act on, not an inbox that reads as "no mail today".
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "you are not authorised for any mailbox - ask IT to add you to the "
            "relevant PW-Mailbox group",
        )
    # `mailboxId` can only ever NARROW the authorised set -- scope.narrow() returns
    # an intersection, never the request itself. A crafted id for a mailbox the user
    # cannot read yields an empty result rather than widening their view.
    stmt = select(Email).where(
        Email.mailbox_id.in_(scope.narrow([mailboxId] if mailboxId else None))
    )
    if not scope.safeguarding:
        # Restricted items are absent from the list, the counts and the search --
        # not shown as a redacted placeholder. A visible "hidden item" marker would
        # itself disclose that a safeguarding matter exists for a family.
        stmt = stmt.where(Email.restricted.is_(False))
    rows = await session.scalars(stmt.order_by(Email.received_at.desc()))
    return [serialize_email(e) for e in rows]


@router.post("/emails/fetch", status_code=status.HTTP_202_ACCEPTED)
async def fetch_emails(
    background_tasks: BackgroundTasks,
    count: int = 0,
    full_resync: bool = False,
    scope: MailboxScope = Depends(mailbox_scope),
    claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)
):
    """Kicks off a fetch+analyze run in the background and returns immediately —
    a full sync can take minutes on a large mailbox (sequential LLM calls, 5 at a
    time), far longer than any request should stay open. The mailbox check below
    stays synchronous so the common failure case still surfaces immediately; the
    slow work is what moved to the background. Poll GET /agent/status (its
    `running` flag, and each history entry's live progress counters) to know how
    it's going — that's real server state, so it stays correct no matter which
    page you're on.

    `count=0` (the default) fetches *everything* not yet synced — a mailbox's
    first-ever sync naturally gets its whole history this way, and every sync
    after that only ever asks for what's actually new (see pipeline.py's sync
    watermark), not a fixed per-run cap. `full_resync=True` ignores the stored
    watermark and rescans the whole mailbox from the start — a recovery/audit
    tool, not something a normal sync needs."""
    mailbox = await session.scalar(select(Mailbox))
    if not mailbox or not mailbox.enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no mailbox configured")

    if pipeline.is_running():
        raise HTTPException(status.HTTP_409_CONFLICT, "a sync is already in progress")

    background_tasks.add_task(pipeline.fetch_and_process, count, sweep=True, reset_watermark=full_resync)
    return {"status": "started"}


@router.post("/emails/move", status_code=status.HTTP_204_NO_CONTENT)
async def move_to_folder(body: MoveIn, claims: dict = Depends(require(WRITE)), scope: MailboxScope = Depends(mailbox_scope), session: AsyncSession = Depends(get_session)):
    rows = await _scoped_emails(session, body.ids, scope)
    for e in rows:
        e.folder_id = body.folderId
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/emails/archive", status_code=status.HTTP_204_NO_CONTENT)
async def archive_emails(body: IdsIn, claims: dict = Depends(require(WRITE)), scope: MailboxScope = Depends(mailbox_scope), session: AsyncSession = Depends(get_session)):
    now = datetime.now(timezone.utc)
    rows = await _scoped_emails(session, body.ids, scope)
    for e in rows:
        e.archived_at = now
        e.archived_by = "user"
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/emails/unarchive", status_code=status.HTTP_204_NO_CONTENT)
async def unarchive_emails(body: IdsIn, claims: dict = Depends(require(WRITE)), scope: MailboxScope = Depends(mailbox_scope), session: AsyncSession = Depends(get_session)):
    rows = await _scoped_emails(session, body.ids, scope)
    for e in rows:
        e.archived_at = None
        e.archived_by = None
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/emails/auto-reply", status_code=status.HTTP_204_NO_CONTENT)
async def generate_drafts(body: IdsIn, claims: dict = Depends(require(WRITE)), scope: MailboxScope = Depends(mailbox_scope), session: AsyncSession = Depends(get_session)):
    """Manual "generate a draft" action. Most emails already get an AI-drafted
    reply at analysis time (see pipeline.py) — this just surfaces it as a draft.
    Rows analyzed before that shipped have no draft_reply yet, so those fall back
    to a fixed template rather than leaving the button a no-op."""
    rows = await _scoped_emails(session, body.ids, scope)
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
async def dismiss_reply(email_id: str, claims: dict = Depends(require(WRITE)), scope: MailboxScope = Depends(mailbox_scope), session: AsyncSession = Depends(get_session)):
    """Human-in-the-loop reject: the AI's draft isn't wanted. The draft stays on
    the row (for audit) but reply_status flips so it drops out of the review queue."""
    e = await _get_email(session, email_id, scope)
    e.reply_status = "rejected"
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/emails/{email_id}/reply", status_code=status.HTTP_204_NO_CONTENT)
async def send_reply(email_id: str, body: ReplyIn, claims: dict = Depends(require(WRITE)), scope: MailboxScope = Depends(mailbox_scope), session: AsyncSession = Depends(get_session)):
    e = await _get_email(session, email_id, scope)
    mailbox = await session.scalar(select(Mailbox))
    if not mailbox:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no mailbox configured")
    send_fn = pipeline._send_fn_for(mailbox.provider)
    send_creds = (
        {
            "tenant_id": mailbox.tenant_id,
            "client_id": mailbox.client_id,
            "client_secret": crypto.decrypt(mailbox.client_secret_enc),
            "mailbox": mailbox.username,
        }
        if mailbox.provider == "graph"
        else {
            "smtp_host": mailbox.smtp_host,
            "smtp_port": mailbox.smtp_port,
            "username": mailbox.username,
            "password": crypto.decrypt(mailbox.password_enc),
        }
    )
    try:
        await run_in_threadpool(
            send_fn, **send_creds, to_addr=e.from_email, subject=f"Re: {e.subject}", body=body.body,
        )
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"send failed: {exc}") from exc
    e.reply_status = "sent"
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/emails/{email_id}/retry-summary")
async def retry_summary(email_id: str, claims: dict = Depends(require(WRITE)), scope: MailboxScope = Depends(mailbox_scope), session: AsyncSession = Depends(get_session)):
    """(Re-)analyzes exactly this one email — the per-email "AI-summarize"
    action, for a "skipped" (never analyzed — see pipeline.py) or "failed" row
    alike. Goes through the same shared analyze_and_persist pipeline.py uses
    everywhere else, so this gets the full result (confidence, needs_reply,
    auto-reply draft, CRM sync, ...), not just summary/category/priority."""
    if pipeline.is_analyzing(email_id):
        # Already being analyzed by another call (a sync's own post-fetch
        # auto-analyze, a backlog catch-up, or a second click landing before the
        # first returned) — reject rather than racing it. Two concurrent
        # attempts would each flip the row to "pending" and run their own LLM
        # call, and whichever commits last wins regardless of which one
        # actually succeeded, so a retry that visibly succeeds could get
        # silently overwritten back to "failed" moments later.
        raise HTTPException(status.HTTP_409_CONFLICT, "this email is already being analyzed")
    e = await _get_email(session, email_id, scope)
    e.summary_status = "pending"
    payload = serialize_email(e)
    await session.commit()

    ctx = await pipeline.load_send_context()
    if ctx is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no mailbox configured")
    send_creds, provider, auto_reply_enabled = ctx
    await pipeline.analyze_and_persist(
        [(email_id, payload)], auto_reply_enabled=auto_reply_enabled, send_creds=send_creds, provider=provider,
    )

    await session.refresh(e)
    return serialize_email(e)


@router.post("/emails/analyze-backlog")
async def analyze_backlog(
    count: int = 100, claims: dict = Depends(require(WRITE)),
    scope: MailboxScope = Depends(mailbox_scope),
    session: AsyncSession = Depends(get_session)
):
    """Settings' "Summarize previous 100" — manually analyzes the next `count`
    not-yet-analyzed emails (see pipeline.py: a sync only auto-analyzes the most
    recent slice, so older imported mail sits "skipped" until asked for).
    Bounded and synchronous (not backgrounded) — a 100-email batch finishes in
    well under the frontend's request timeout, so the button can just show its
    own "summarizing…" state and get a direct result back, no polling needed."""
    try:
        result = await pipeline.analyze_backlog(count)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return result


# ---------- folders ----------
@router.get("/folders")
async def list_folders(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    rows = await session.scalars(select(Folder).order_by(Folder.created_at))
    return [serialize_folder(f) for f in rows]


@router.post("/folders", status_code=status.HTTP_201_CREATED)
async def create_folder(body: FolderIn, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    folder = Folder(name=body.name, color=body.color)
    session.add(folder)
    await session.commit()
    return serialize_folder(folder)


@router.put("/folders/{folder_id}")
async def update_folder(folder_id: str, body: FolderPatch, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    f = await session.scalar(select(Folder).where(Folder.id == folder_id))
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
    emails = await session.scalars(select(Email).where(Email.folder_id == folder_id))
    for e in emails:
        e.folder_id = None
    f = await session.scalar(select(Folder).where(Folder.id == folder_id))
    if f:
        await session.delete(f)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- alerts ----------
@router.get("/alerts")
async def list_alerts(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    # Filter in SQL rather than pulling every email (body/summary/draft_reply
    # included) over the wire just to discard most of them in Python.
    #
    # Three things raise an alert, per the brief: a safeguarding matter (§5.5 — an
    # "immediate restricted alert"), an item that could not be classified and so
    # needs a person (§5.8 exception queue), and anything High priority. The
    # safeguarding test is on `route` rather than `category` because the
    # cross-cutting indicator sends an item down the restricted route even when
    # its primary category is something else entirely.
    rows = await session.scalars(
        select(Email).where(
            or_(
                Email.priority == "High",
                Email.route == "safeguarding_restricted",
                Email.route == "exception",
            )
        )
    )
    alerts = []
    for e in rows:
        restricted = e.route == "safeguarding_restricted"
        exception = e.route == "exception"
        if restricted:
            # Deliberately says nothing about the content. §7.2 restricts
            # safeguarding information to authorised staff, and Appendix E warns
            # against exposing sensitive detail in alerts — so the alert carries
            # a pointer, and the email itself stays behind the normal permission
            # check. The subject line is withheld for the same reason.
            alert_type, message = "safeguarding", "Restricted safeguarding item requires attention"
        elif exception:
            alert_type = "exception"
            message = f'Could not be classified, needs review: "{e.subject}"'
        else:
            alert_type, message = "priority", f'High-priority: "{e.subject}"'
        alerts.append({
            "id": f"a-{e.id}",
            "emailId": e.id,
            "type": alert_type,
            "message": message,
            "priority": "High" if restricted else e.priority,
            "flags": list(e.flags or []),
            "createdAt": e.received_at.isoformat() if e.received_at else None,
            "archived": e.archived_at is not None,
        })
    return alerts


# ---------- agent status ----------
async def _get_config(session: AsyncSession) -> AgentConfig:
    cfg = await session.scalar(select(AgentConfig))
    if not cfg:
        cfg = AgentConfig(fetch_per_run=settings.fetch_per_run, enabled=settings.cron_enabled)
        session.add(cfg)
        await session.commit()
    return cfg


@router.get("/agent/status")
async def agent_status(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    cfg = await _get_config(session)
    runs = list(await session.scalars(select(AgentRun).order_by(AgentRun.run_at.desc())))
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
        "running": pipeline.is_running(),
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
    cfg = await _get_config(session)
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
    mb = await session.scalar(select(Mailbox))
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
    mailbox = await session.scalar(select(Mailbox))
    enc = crypto.encrypt(body.password.replace(" ", ""))  # app passwords are shown spaced; store without
    # clientSecret is write-only-if-changed: a blank value keeps whatever was already stored.
    client_secret_enc = (
        crypto.encrypt(body.clientSecret) if body.clientSecret
        else (mailbox.client_secret_enc if mailbox else None)
    )
    if mailbox:
        # Changing which mailbox we're pointed at (a different account/host, or a
        # provider switch) makes the stored sync watermark meaningless — it was
        # scoped to the *old* mailbox's UID numbering (IMAP) or timestamp cursor
        # (Graph). Left in place, the next sync narrows its search by that stale
        # cursor and comes back looking like nothing new ever arrived, even though
        # the new mailbox has never actually been synced. A password-only change
        # (import stays on the same account) must NOT reset this — that would
        # force a needless full rescan on every credential rotation.
        identity_changed = (
            mailbox.provider != body.provider
            or mailbox.username != body.username
            or (body.provider == "graph" and (
                mailbox.tenant_id != body.tenantId or mailbox.client_id != body.clientId
            ))
            or (body.provider != "graph" and mailbox.imap_host != body.imapHost)
        )
        mailbox.imap_host, mailbox.imap_port = body.imapHost, body.imapPort
        mailbox.smtp_host, mailbox.smtp_port = body.smtpHost, body.smtpPort
        mailbox.username, mailbox.password_enc, mailbox.enabled = body.username, enc, True
        mailbox.provider = body.provider
        mailbox.tenant_id, mailbox.client_id, mailbox.client_secret_enc = body.tenantId, body.clientId, client_secret_enc
        if identity_changed:
            mailbox.last_synced_uid = None
            mailbox.last_synced_at = None
            mailbox.uid_validity = None
            # New generation — see Mailbox.sync_epoch's docstring for why this
            # can't just rely on the provider reporting a fresh UIDVALIDITY.
            mailbox.sync_epoch = (mailbox.sync_epoch or 0) + 1
    else:
        session.add(Mailbox(
            imap_host=body.imapHost, imap_port=body.imapPort,
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
