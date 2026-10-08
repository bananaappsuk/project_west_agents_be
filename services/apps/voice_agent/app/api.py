from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import agent_client, bt_client, crm_sync, crypto, pipeline, s3_client, transcribe
from .config import settings
from .db import get_session
from .deps import require
from .models import AgentRun, Notification, Recording, VoiceSettings
from .schemas import (
    RelabelIn,
    ReplyDraftIn,
    SettingsIn,
    serialize_notification,
    serialize_recording,
)

log = logging.getLogger("voice_agent.api")

# All voice endpoints live under /voice so they never collide with the mail agent's
# /settings, /agent, etc. at the gateway.
router = APIRouter(prefix="/voice")

READ = f"{settings.app_key}:recordings.read"
WRITE = f"{settings.app_key}:recordings.write"

_FREQ_MINUTES = {"hourly": 60, "every6h": 360, "daily": 1440}


async def _get_recording(session: AsyncSession, rec_id: str) -> Recording:
    r = await session.scalar(select(Recording).where(Recording.id == rec_id))
    if not r:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "recording not found")
    return r


async def _get_config(session: AsyncSession) -> VoiceSettings:
    """Find-or-create the VoiceSettings row. Mirrors mail_agent's `_get_config`:
    the master on/off switch (`enabled`) defaults on the moment the agent is first
    looked at, independent of whether a real recording-source connection has been
    configured yet — same as Email Agent showing "Running" before a mailbox is set
    up. The scheduler already no-ops gracefully on an enabled-but-unconfigured agent
    (see scheduler.py's "enabled but no endpoint configured — skipping")."""
    cfg = await session.scalar(select(VoiceSettings))
    if not cfg:
        cfg = VoiceSettings(enabled=settings.cron_enabled)
        session.add(cfg)
        await session.commit()
    return cfg


# ---------- recordings ----------
@router.get("/recordings")
async def list_recordings(
    status_filter: str = Query("all", alias="status"),
    claims: dict = Depends(require(READ)),
    session: AsyncSession = Depends(get_session),
):
    q = select(Recording).order_by(Recording.call_date.desc())
    if status_filter in ("new", "old"):
        q = q.where(Recording.status == status_filter)
    rows = await session.scalars(q)
    return [serialize_recording(r) for r in rows]


@router.get("/alerts")
async def list_alerts(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    """Mirrors mail_agent's GET /alerts: high-priority and high-risk calls, not a
    separate table — recomputed from the recordings themselves each time so it can
    never drift from what /voice/recordings actually shows."""
    # Filter in SQL rather than pulling every recording (transcript/summary/
    # ai_reply included) over the wire just to discard most of them in Python.
    # Matches mail_agent's /alerts now that both channels share one taxonomy: a
    # safeguarding call must raise the same restricted alert as a safeguarding
    # email (brief §5.5), and an unclassifiable one goes to the same exception
    # queue (§5.8). The safeguarding test is on `route`, not `category`, because
    # the cross-cutting indicator sends an item down the restricted route even
    # when its primary category is something else.
    rows = await session.scalars(
        select(Recording).where(
            or_(
                Recording.priority == "High",
                Recording.risk == "High",
                Recording.route == "safeguarding_restricted",
                Recording.route == "exception",
            ),
        )
    )
    alerts = []
    for r in rows:
        restricted = r.route == "safeguarding_restricted"
        title = r.label or r.caller or "Unknown caller"
        if restricted:
            # Carries no detail about the call, for the same reason the mail
            # equivalent withholds the subject line: §7.2 restricts safeguarding
            # information to authorised staff, so the alert is a pointer and the
            # recording itself stays behind the normal permission check.
            alert_type, message = "safeguarding", "Restricted safeguarding call requires attention"
        elif r.route == "exception":
            alert_type, message = "exception", f'Call could not be classified, needs review: "{title}"'
        elif r.risk == "High":
            alert_type, message = "risk", f'High-risk call flagged: "{title}"'
        else:
            alert_type, message = "priority", f'High-priority call: "{title}"'
        alerts.append({
            "id": f"a-{r.id}",
            "recordingId": r.id,
            "type": alert_type,
            "message": message,
            "priority": "High" if restricted else r.priority,
            "flags": list(r.flags or []),
            "createdAt": r.created_at.isoformat() if r.created_at else None,
        })
    return alerts


@router.get("/actions")
async def list_actions(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    """Backs the dashboard's "Actions to take" card: a focused list of recordings that
    need a human decision right now — an AI-drafted reply still awaiting approval, or a
    call flagged high-risk — as opposed to /alerts (broader: any high-priority OR
    high-risk call) or /stats (aggregate counts only, no actual items). Column-projected
    like /alerts and /stats, not full rows — see the comment on /alerts for why."""
    rows = list(await session.execute(
        select(
            Recording.id, Recording.label, Recording.caller, Recording.call_date,
            Recording.category, Recording.priority, Recording.risk,
            Recording.reply_status, Recording.needs_reply,
        ).where(
            or_(
                and_(Recording.needs_reply.is_(True), Recording.reply_status.in_(["pending", "edited"])),
                Recording.risk == "High",
            ),
        ).order_by(Recording.call_date.desc())
    ))
    items = []
    for r in rows:
        reasons = []
        if r.needs_reply and r.reply_status in ("pending", "edited"):
            reasons.append("reply")
        if r.risk == "High":
            reasons.append("risk")
        items.append({
            "id": r.id,
            "title": r.label or r.caller or "Unknown caller",
            "date": r.call_date,
            "category": r.category,
            "priority": r.priority,
            "risk": r.risk,
            "replyStatus": r.reply_status,
            "reasons": reasons,
        })
    return {"count": len(items), "items": items[:20]}


@router.post("/recordings/fetch", status_code=status.HTTP_202_ACCEPTED)
async def fetch_recordings(
    background_tasks: BackgroundTasks,
    count: int = 12,
    sweep: bool = True,
    claims: dict = Depends(require(WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Kicks off a fetch+analyze run in the background and returns immediately —
    a full sync can take minutes on a large batch (sequential LLM calls, 5 at a
    time), far longer than any request should stay open. Poll GET /voice/agent/status
    (its `running` flag) to know when it's done — that's real server state, so it
    stays correct no matter which page you're on."""
    cfg = await session.scalar(select(VoiceSettings))
    if not cfg or not cfg.enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no recording source configured")

    if pipeline.is_running():
        raise HTTPException(status.HTTP_409_CONFLICT, "a sync is already in progress")

    background_tasks.add_task(pipeline.fetch_and_process, count, sweep=sweep)
    return {"status": "started"}


@router.post("/recordings/upload")
async def upload_recording(
    file: UploadFile = File(...),
    save_audio: bool = Form(False),
    label: str | None = Form(None),
    claims: dict = Depends(require(WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Hand the agent an audio file directly instead of waiting on the cron sync.
    Reuses the same transcription + analysis steps the BT Cloud/S3 pipeline uses.
    `save_audio` opts into keeping the file (in the already-configured S3-compatible
    bucket) for playback afterward — the default mirrors the rest of the product's
    discard-after-transcribe policy, so BT-Cloud-style recordings with no playback."""
    audio = await file.read()
    content_type = file.content_type or "audio/mpeg"
    transcript = await run_in_threadpool(transcribe.transcribe, audio, content_type)

    source_type, ext_id = "upload", str(uuid.uuid4())
    if save_audio:
        cfg = await session.scalar(select(VoiceSettings))
        if not cfg or not cfg.s3_bucket:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "no S3-compatible storage configured — set one up in Settings to save audio",
            )
        ext = "wav" if "wav" in content_type else "mp3"
        key = f"uploads/{ext_id}.{ext}"
        try:
            await run_in_threadpool(
                s3_client.upload_object,
                endpoint=cfg.s3_endpoint, region=cfg.s3_region, bucket=cfg.s3_bucket,
                access_key_id=cfg.s3_access_key_id,
                secret_access_key=crypto.decrypt(cfg.s3_secret_access_key_enc) if cfg.s3_secret_access_key_enc else "",
                key=key, body=audio, content_type=content_type,
            )
        except Exception as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"could not save audio: {exc}") from exc
        source_type, ext_id = "s3", key

    caller = file.filename or "Uploaded recording"
    try:
        res = await agent_client.analyze_call({
            "caller": caller, "phone": "", "agent": "Uploaded", "duration": "", "transcript": transcript,
        })
        a = res.get("analysis") or {}
        analysis_status = "done"
    except Exception as exc:
        log.warning("upload analysis FAILED: %s", exc)
        a, analysis_status = {}, "failed"

    row = Recording(
        ext_id=ext_id, source_type=source_type,
        label=(label or "").strip() or None,
        caller=caller, phone="", agent="Uploaded",
        call_date=date.today().isoformat(), duration="",
        transcript=transcript or "(no transcript available)",
        summary=a.get("summary", ""), category=a.get("category") or "unclassified",
        flags=list(a.get("flags") or []),
        route=a.get("route") or "exception",
        priority=a.get("priority", "Medium"), risk=a.get("risk", "Low"), sentiment=a.get("sentiment", "Neutral"),
        needs_reply=bool(a.get("needs_reply", False)), ai_reply=a.get("suggested_reply", "") or "",
        reply_status="pending" if a.get("needs_reply") else "none",
        analysis_status=analysis_status, status="new",
    )
    session.add(row)
    await session.commit()
    return serialize_recording(row)


@router.get("/recordings/{rec_id}")
async def get_recording(rec_id: str, claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    return serialize_recording(await _get_recording(session, rec_id))


@router.patch("/recordings/{rec_id}/label")
async def relabel_recording(
    rec_id: str, body: RelabelIn, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)
):
    """Rename a recording's display name — purely cosmetic (doesn't touch `caller`,
    which for a real call is the actual caller ID, not something to relabel)."""
    r = await _get_recording(session, rec_id)
    trimmed = body.label.strip()
    r.label = trimmed or None
    await session.commit()
    return serialize_recording(r)


async def _fetch_recording_audio(cfg: VoiceSettings | None, r: Recording) -> tuple[bytes, str]:
    """Re-fetches a recording's audio from its source on demand — nothing is stored
    locally (see pipeline.py: audio is discarded right after transcription). S3-sourced
    recordings are re-fetched from the bucket; BT Cloud recordings are re-fetched from
    RingCentral using a fresh/cached access token (see bt_client.fetch_audio) — only
    possible for recordings synced after content_uri started being captured, with a JWT
    still configured. Shared by GET .../audio (playback) and
    POST .../retry-analysis (re-transcribing a recording whose transcript never came
    through)."""
    if r.source_type == "s3":
        if not cfg or not cfg.s3_bucket:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no S3-compatible source configured")
        try:
            return await run_in_threadpool(
                s3_client.download,
                endpoint=cfg.s3_endpoint, region=cfg.s3_region, bucket=cfg.s3_bucket,
                access_key_id=cfg.s3_access_key_id,
                secret_access_key=crypto.decrypt(cfg.s3_secret_access_key_enc) if cfg.s3_secret_access_key_enc else "",
                key=r.ext_id,
            )
        except Exception as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"could not fetch audio: {exc}") from exc
    elif r.source_type == "bt_cloud":
        jwt = crypto.decrypt(cfg.jwt_enc) if (cfg and cfg.jwt_enc) else ""
        if not r.content_uri or not jwt:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "audio not available for this recording")
        try:
            return await run_in_threadpool(
                bt_client.fetch_audio,
                endpoint=cfg.endpoint, client_id=cfg.client_id,
                client_secret=crypto.decrypt(cfg.client_secret_enc) if cfg.client_secret_enc else "",
                jwt=jwt, content_uri=r.content_uri,
            )
        except Exception as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"could not fetch audio: {exc}") from exc
    else:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "audio not available for this recording")


@router.get("/recordings/{rec_id}/audio")
async def get_recording_audio(rec_id: str, claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    r = await _get_recording(session, rec_id)
    cfg = await session.scalar(select(VoiceSettings))
    audio, content_type = await _fetch_recording_audio(cfg, r)
    return Response(content=audio, media_type=content_type)


@router.post("/recordings/{rec_id}/retry-analysis")
async def retry_analysis(rec_id: str, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    """(Re-)transcribes (if the transcript never came through) and (re-)analyzes
    exactly this one recording — the per-call "Generate summary" action, for a
    call whose transcript and/or summary is missing (source unreachable at sync
    time, the LLM call failed, OPENAI_API_KEY unset when it was first synced, ...).
    Re-fetches audio on demand the same way GET .../audio does — nothing is stored
    locally between syncs. Goes through the same fields the sync pipeline persists
    (summary/category/..., CRM sync), not just a bare re-transcribe."""
    if not pipeline.try_claim_analyzing(rec_id):
        # Already being analyzed by another call (the sync's own post-fetch
        # analyze step re-queuing this same stuck-pending row, or a second click
        # landing before the first returned) — reject rather than racing it. See
        # pipeline.py's `_analyzing` docstring for why a race here would let a
        # retry that visibly succeeds get silently overwritten back to "failed".
        raise HTTPException(status.HTTP_409_CONFLICT, "this recording is already being analyzed")
    try:
        r = await _get_recording(session, rec_id)
        cfg = await session.scalar(select(VoiceSettings))

        transcript = r.transcript
        if not transcript or transcript == "(no transcript available)":
            audio, content_type = await _fetch_recording_audio(cfg, r)
            transcript = await run_in_threadpool(transcribe.transcribe, audio, content_type)
            if not transcript:
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "transcription returned no text for this recording")
            r.transcript = transcript

        r.analysis_status = "pending"
        await session.commit()

        payload = {
            "ext_id": r.ext_id, "call_date": r.call_date, "caller": r.caller,
            "phone": r.phone, "agent": r.agent, "duration": r.duration, "transcript": transcript,
        }
        try:
            res = await agent_client.analyze_call(payload)
            a = res.get("analysis") or {}
            ok = True
        except Exception as exc:
            log.warning("retry-analysis FAILED rec=%s: %s", rec_id, exc)
            a, ok = {}, False

        if ok:
            crm_status, crm_ref, activity_ref = await crm_sync.after_call_analysis(payload, a)
            r.summary = a.get("summary", "")
            r.category = a.get("category") or "unclassified"
            r.flags = list(a.get("flags") or [])
            r.route = a.get("route") or "exception"
            r.priority = a.get("priority", "Medium")
            r.risk = a.get("risk", "Low")
            r.sentiment = a.get("sentiment", "Neutral")
            r.needs_reply = bool(a.get("needs_reply", False))
            r.ai_reply = a.get("suggested_reply", "") or ""
            r.reply_status = "pending" if r.needs_reply else "none"
            r.analysis_status = "done"
            r.intent = a.get("intent") or "NONE"
            r.crm_status, r.crm_reference, r.activity_ref = crm_status, crm_ref, activity_ref
        else:
            r.analysis_status = "failed"
        await session.commit()

        await session.refresh(r)
        return serialize_recording(r)
    finally:
        pipeline.release_analyzing(rec_id)


# ---------- human-in-the-loop reply ----------
@router.post("/recordings/{rec_id}/reply/draft")
async def save_reply_draft(rec_id: str, body: ReplyDraftIn, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    r = await _get_recording(session, rec_id)
    r.ai_reply = body.body
    r.reply_status = "edited"
    await session.commit()
    return serialize_recording(r)


async def _set_reply_status(rec_id: str, new_status: str, session: AsyncSession) -> dict:
    r = await _get_recording(session, rec_id)
    r.reply_status = new_status
    await session.commit()
    return serialize_recording(r)


@router.post("/recordings/{rec_id}/reply/approve")
async def approve_reply(rec_id: str, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    return await _set_reply_status(rec_id, "approved", session)


@router.post("/recordings/{rec_id}/reply/reject")
async def reject_reply(rec_id: str, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    return await _set_reply_status(rec_id, "rejected", session)


@router.post("/recordings/{rec_id}/reply/send")
async def send_reply(rec_id: str, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    return await _set_reply_status(rec_id, "sent", session)


# ---------- dashboard stats ----------
@router.get("/stats")
async def stats(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    # Only the small classification columns these counts need — not full rows,
    # which would drag every recording's transcript/summary/ai_reply (the
    # largest columns in the schema) over the wire just to tally counters.
    rows = list(await session.execute(
        select(
            Recording.status, Recording.reply_status, Recording.risk,
            Recording.category, Recording.sentiment, Recording.agent,
        )
    ))
    cfg = await _get_config(session)

    # The controlled PW taxonomy (agent_factory/app/taxonomy.py). Listed here rather
    # than imported because the agent factory is a separate service — the frontend
    # holds the display labels, so this only needs the codes, in brief Reference 1's
    # own order. A breakdown row is emitted even at zero so the chart keeps a stable
    # set of series between refreshes.
    categories = [
        "general_enquiry", "new_referral", "referral_update", "returned_part_2",
        "case_correspondence", "payment_or_finance", "complaint", "safeguarding_concern",
        "report_or_document", "scheduling_change", "court_or_legal", "supplier_or_admin",
        "unclassified",
    ]
    sentiments = ["Positive", "Neutral", "Negative"]
    agents = sorted({r.agent for r in rows if r.agent})

    return {
        "total": len(rows),
        "new": sum(1 for r in rows if r.status == "new"),
        "pending": sum(1 for r in rows if r.reply_status == "pending"),
        "highRisk": sum(1 for r in rows if r.risk == "High"),
        "syncStatus": "Healthy" if cfg.enabled else "Down",
        "categoryBreakdown": [{"name": c, "count": sum(1 for r in rows if r.category == c)} for c in categories],
        "sentimentBreakdown": [{"name": s, "count": sum(1 for r in rows if r.sentiment == s)} for s in sentiments],
        "agentPerformance": [
            {
                "name": a,
                "total": sum(1 for r in rows if r.agent == a),
                "pending": sum(1 for r in rows if r.agent == a and r.reply_status == "pending"),
                "highRisk": sum(1 for r in rows if r.agent == a and r.risk == "High"),
            }
            for a in agents
        ],
    }


# ---------- settings (recording source connection + cron) ----------
@router.get("/settings")
async def get_settings(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    """Non-secret settings for prefilling the form. Secrets are never returned. Both source
    field sets are always returned — one may be configured but currently pointed
    at the other, and the form shouldn't lose it when the admin isn't looking at that tab."""
    cfg = await session.scalar(select(VoiceSettings))
    if not cfg:
        return None
    return {
        "sourceType": cfg.source_type,
        "endpoint": cfg.endpoint,
        "clientId": cfg.client_id,
        "clientSecret": "",  # redacted
        "jwt": "",           # redacted
        "secretConfigured": bool(cfg.client_secret_enc),
        "jwtConfigured": bool(cfg.jwt_enc),
        "s3Endpoint": cfg.s3_endpoint,
        "s3Region": cfg.s3_region,
        "s3Bucket": cfg.s3_bucket,
        "s3Prefix": cfg.s3_prefix,
        "s3AccessKeyId": cfg.s3_access_key_id,
        "s3SecretAccessKey": "",  # redacted
        "s3SecretConfigured": bool(cfg.s3_secret_access_key_enc),
        "cronFrequency": cfg.cron_frequency,
        "cronTime": cfg.cron_time,
        "enabled": cfg.enabled,
        "configured": True,
    }


@router.post("/settings")
async def save_settings(body: SettingsIn, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    cfg = await session.scalar(select(VoiceSettings))
    # Blank secret / jwt / s3 secret on save means "keep the stored one".
    enc = crypto.encrypt(body.clientSecret) if body.clientSecret else (cfg.client_secret_enc if cfg else "")
    jwt_enc = crypto.encrypt(body.jwt) if body.jwt else (cfg.jwt_enc if cfg else "")
    s3_secret_enc = crypto.encrypt(body.s3SecretAccessKey) if body.s3SecretAccessKey else (cfg.s3_secret_access_key_enc if cfg else "")
    if cfg:
        cfg.source_type = body.sourceType
        cfg.endpoint, cfg.client_id, cfg.client_secret_enc, cfg.jwt_enc = body.endpoint, body.clientId, enc, jwt_enc
        cfg.s3_endpoint, cfg.s3_region, cfg.s3_bucket = body.s3Endpoint, body.s3Region, body.s3Bucket
        cfg.s3_prefix, cfg.s3_access_key_id, cfg.s3_secret_access_key_enc = body.s3Prefix, body.s3AccessKeyId, s3_secret_enc
        cfg.cron_frequency, cfg.cron_time, cfg.enabled = body.cronFrequency, body.cronTime, body.enabled
    else:
        session.add(VoiceSettings(
            source_type=body.sourceType,
            endpoint=body.endpoint, client_id=body.clientId, client_secret_enc=enc, jwt_enc=jwt_enc,
            s3_endpoint=body.s3Endpoint, s3_region=body.s3Region, s3_bucket=body.s3Bucket,
            s3_prefix=body.s3Prefix, s3_access_key_id=body.s3AccessKeyId, s3_secret_access_key_enc=s3_secret_enc,
            cron_frequency=body.cronFrequency, cron_time=body.cronTime, enabled=body.enabled,
        ))
    await session.commit()
    return {"ok": True}


@router.post("/settings/test")
async def test_settings(body: SettingsIn, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    cfg = await session.scalar(select(VoiceSettings))
    try:
        if body.sourceType == "s3":
            secret = body.s3SecretAccessKey or (crypto.decrypt(cfg.s3_secret_access_key_enc) if (cfg and cfg.s3_secret_access_key_enc) else "")
            mode = await run_in_threadpool(
                s3_client.test_connection,
                endpoint=body.s3Endpoint, region=body.s3Region, bucket=body.s3Bucket,
                prefix=body.s3Prefix, access_key_id=body.s3AccessKeyId, secret_access_key=secret,
            )
        else:
            secret = body.clientSecret or (crypto.decrypt(cfg.client_secret_enc) if (cfg and cfg.client_secret_enc) else "")
            jwt = body.jwt or (crypto.decrypt(cfg.jwt_enc) if (cfg and cfg.jwt_enc) else "")
            # With a JWT this performs a real RingCentral/BT Cloud Work auth; without one it
            # just validates the fields (demo mode).
            mode = await run_in_threadpool(
                bt_client.test_connection,
                endpoint=body.endpoint, client_id=body.clientId, client_secret=secret, jwt=jwt,
            )
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"connection failed: {exc}") from exc
    return {"ok": True, "mode": mode}


# ---------- notifications ----------
@router.get("/notifications")
async def list_notifications(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    rows = await session.scalars(
        select(Notification).order_by(Notification.created_at.desc()).limit(30)
    )
    return [serialize_notification(n) for n in rows]


@router.post("/notifications/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_read(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    rows = await session.scalars(select(Notification).where(Notification.read.is_(False)))
    for n in rows:
        n.read = True
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- agent / cron status ----------
@router.get("/agent/status")
async def agent_status(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    cfg = await _get_config(session)
    runs = list(await session.scalars(select(AgentRun).order_by(AgentRun.run_at.desc())))
    last = runs[0] if runs else None

    daily: dict[str, dict] = {}
    for r in runs:
        day = r.run_at.date().isoformat()
        d = daily.setdefault(day, {"day": day, "processed": 0, "highRisk": 0})
        d["processed"] += r.processed
        d["highRisk"] += r.high_risk

    next_run = ""
    if cfg.enabled and last:
        interval = _FREQ_MINUTES.get(cfg.cron_frequency, 360)
        next_run = (last.run_at + timedelta(minutes=interval)).isoformat()

    return {
        "running": pipeline.is_running(),
        "config": {
            "cronFrequency": cfg.cron_frequency,
            "cronTime": cfg.cron_time,
            "enabled": cfg.enabled,
        },
        "stats": {
            "totalRuns": len(runs),
            "lastRunAt": last.run_at.isoformat() if last else "",
            "nextRunAt": next_run,
            "lastFetchCount": last.fetched if last else 0,
            "totalFetched": sum(r.fetched for r in runs),
            # See mail_agent's api.py for why this is `processed`, not `fetched`:
            # "fetched" is the raw per-run scan count, which can re-scan the same
            # window run after run with nothing new — "processed" is genuinely new.
            "totalProcessed": sum(r.processed for r in runs),
            "totalHighRisk": sum(r.high_risk for r in runs),
        },
        "history": [
            {"runAt": r.run_at.isoformat(), "fetched": r.fetched, "processed": r.processed,
             "highRisk": r.high_risk, "status": r.status, "errorMessage": r.error_message}
            for r in runs[:5]
        ],
        "daily": sorted(daily.values(), key=lambda d: d["day"])[-7:],
    }
