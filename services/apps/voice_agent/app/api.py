from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import agent_client, bt_client, crypto, pipeline, s3_client, transcribe
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


def _org(claims: dict) -> str:
    return claims["org"]


async def _get_recording(session: AsyncSession, org: str, rec_id: str) -> Recording:
    r = await session.scalar(select(Recording).where(Recording.org_id == org, Recording.id == rec_id))
    if not r:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "recording not found")
    return r


async def _get_config(session: AsyncSession, org: str) -> VoiceSettings:
    """Find-or-create the org's VoiceSettings row. Mirrors mail_agent's `_get_config`:
    the master on/off switch (`enabled`) defaults on the moment the agent is first
    looked at, independent of whether a real recording-source connection has been
    configured yet — same as Email Agent showing "Running" before a mailbox is set
    up. The scheduler already no-ops gracefully on an enabled-but-unconfigured org
    (see scheduler.py's "enabled but no endpoint configured — skipping")."""
    cfg = await session.scalar(select(VoiceSettings).where(VoiceSettings.org_id == org))
    if not cfg:
        cfg = VoiceSettings(org_id=org, enabled=settings.cron_enabled)
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
    q = select(Recording).where(Recording.org_id == _org(claims)).order_by(Recording.call_date.desc())
    if status_filter in ("new", "old"):
        q = q.where(Recording.status == status_filter)
    rows = await session.scalars(q)
    return [serialize_recording(r) for r in rows]


@router.get("/alerts")
async def list_alerts(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    """Mirrors mail_agent's GET /alerts: high-priority and high-risk calls, not a
    separate table — recomputed from the recordings themselves each time so it can
    never drift from what /voice/recordings actually shows."""
    rows = await session.scalars(select(Recording).where(Recording.org_id == _org(claims)))
    alerts = []
    for r in rows:
        is_high_risk = r.risk == "High"
        if r.priority == "High" or is_high_risk:
            title = r.label or r.caller or "Unknown caller"
            alerts.append({
                "id": f"a-{r.id}",
                "recordingId": r.id,
                "type": "risk" if is_high_risk else "priority",
                "message": (f'High-risk call flagged: "{title}"' if is_high_risk
                            else f'High-priority call: "{title}"'),
                "priority": r.priority,
                "createdAt": r.created_at.isoformat() if r.created_at else None,
            })
    return alerts


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
    org = _org(claims)
    cfg = await session.scalar(select(VoiceSettings).where(VoiceSettings.org_id == org))
    if not cfg or not cfg.enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no recording source configured for this org")

    if pipeline.is_running(org):
        raise HTTPException(status.HTTP_409_CONFLICT, "a sync is already in progress")

    background_tasks.add_task(pipeline.fetch_and_process, org, count, sweep=sweep)
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
    `save_audio` opts into keeping the file (in the org's already-configured S3-compatible
    bucket) for playback afterward — the default mirrors the rest of the product's
    discard-after-transcribe policy, so BT-Cloud-style recordings with no playback."""
    org = _org(claims)
    audio = await file.read()
    content_type = file.content_type or "audio/mpeg"
    transcript = await run_in_threadpool(transcribe.transcribe, audio, content_type)

    source_type, ext_id = "upload", str(uuid.uuid4())
    if save_audio:
        cfg = await session.scalar(select(VoiceSettings).where(VoiceSettings.org_id == org))
        if not cfg or not cfg.s3_bucket:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "no S3-compatible storage configured for this org — set one up in Settings to save audio",
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
        org_id=org, ext_id=ext_id, source_type=source_type,
        label=(label or "").strip() or None,
        caller=caller, phone="", agent="Uploaded",
        call_date=date.today().isoformat(), duration="",
        transcript=transcript or "(no transcript available)",
        summary=a.get("summary", ""), category=a.get("category", "General Enquiry"),
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
    return serialize_recording(await _get_recording(session, _org(claims), rec_id))


@router.patch("/recordings/{rec_id}/label")
async def relabel_recording(
    rec_id: str, body: RelabelIn, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)
):
    """Rename a recording's display name — purely cosmetic (doesn't touch `caller`,
    which for a real call is the actual caller ID, not something to relabel)."""
    r = await _get_recording(session, _org(claims), rec_id)
    trimmed = body.label.strip()
    r.label = trimmed or None
    await session.commit()
    return serialize_recording(r)


@router.get("/recordings/{rec_id}/audio")
async def get_recording_audio(rec_id: str, claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    """Re-fetches the recording's audio from the bucket on demand — nothing is
    stored locally (see pipeline.py: audio is discarded right after transcription).
    Only S3-sourced recordings have anything to serve; BT Cloud's content URL needs
    a short-lived RingCentral token we never retain, so those 404 here."""
    org = _org(claims)
    r = await _get_recording(session, org, rec_id)
    if r.source_type != "s3":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "audio not available for this recording")
    cfg = await session.scalar(select(VoiceSettings).where(VoiceSettings.org_id == org))
    if not cfg or not cfg.s3_bucket:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no S3-compatible source configured for this org")
    try:
        audio, content_type = await run_in_threadpool(
            s3_client.download,
            endpoint=cfg.s3_endpoint, region=cfg.s3_region, bucket=cfg.s3_bucket,
            access_key_id=cfg.s3_access_key_id,
            secret_access_key=crypto.decrypt(cfg.s3_secret_access_key_enc) if cfg.s3_secret_access_key_enc else "",
            key=r.ext_id,
        )
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"could not fetch audio: {exc}") from exc
    return Response(content=audio, media_type=content_type)


# ---------- human-in-the-loop reply ----------
@router.post("/recordings/{rec_id}/reply/draft")
async def save_reply_draft(rec_id: str, body: ReplyDraftIn, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    r = await _get_recording(session, _org(claims), rec_id)
    r.ai_reply = body.body
    r.reply_status = "edited"
    await session.commit()
    return serialize_recording(r)


async def _set_reply_status(rec_id: str, org: str, new_status: str, session: AsyncSession) -> dict:
    r = await _get_recording(session, org, rec_id)
    r.reply_status = new_status
    await session.commit()
    return serialize_recording(r)


@router.post("/recordings/{rec_id}/reply/approve")
async def approve_reply(rec_id: str, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    return await _set_reply_status(rec_id, _org(claims), "approved", session)


@router.post("/recordings/{rec_id}/reply/reject")
async def reject_reply(rec_id: str, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    return await _set_reply_status(rec_id, _org(claims), "rejected", session)


@router.post("/recordings/{rec_id}/reply/send")
async def send_reply(rec_id: str, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    return await _set_reply_status(rec_id, _org(claims), "sent", session)


# ---------- dashboard stats ----------
@router.get("/stats")
async def stats(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    org = _org(claims)
    rows = list(await session.scalars(select(Recording).where(Recording.org_id == org)))
    cfg = await _get_config(session, org)

    categories = ["Sales Enquiry", "Complaint", "Support", "Booking", "Billing", "General Enquiry"]
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
    field sets are always returned — the org may have one configured but currently be pointed
    at the other, and the form shouldn't lose it when the admin isn't looking at that tab."""
    cfg = await session.scalar(select(VoiceSettings).where(VoiceSettings.org_id == _org(claims)))
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
    org = _org(claims)
    cfg = await session.scalar(select(VoiceSettings).where(VoiceSettings.org_id == org))
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
            org_id=org, source_type=body.sourceType,
            endpoint=body.endpoint, client_id=body.clientId, client_secret_enc=enc, jwt_enc=jwt_enc,
            s3_endpoint=body.s3Endpoint, s3_region=body.s3Region, s3_bucket=body.s3Bucket,
            s3_prefix=body.s3Prefix, s3_access_key_id=body.s3AccessKeyId, s3_secret_access_key_enc=s3_secret_enc,
            cron_frequency=body.cronFrequency, cron_time=body.cronTime, enabled=body.enabled,
        ))
    await session.commit()
    return {"ok": True}


@router.post("/settings/test")
async def test_settings(body: SettingsIn, claims: dict = Depends(require(WRITE)), session: AsyncSession = Depends(get_session)):
    cfg = await session.scalar(select(VoiceSettings).where(VoiceSettings.org_id == _org(claims)))
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
        select(Notification).where(Notification.org_id == _org(claims)).order_by(Notification.created_at.desc()).limit(30)
    )
    return [serialize_notification(n) for n in rows]


@router.post("/notifications/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_read(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    rows = await session.scalars(select(Notification).where(Notification.org_id == _org(claims), Notification.read.is_(False)))
    for n in rows:
        n.read = True
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- agent / cron status ----------
@router.get("/agent/status")
async def agent_status(claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    org = _org(claims)
    cfg = await _get_config(session, org)
    runs = list(await session.scalars(select(AgentRun).where(AgentRun.org_id == org).order_by(AgentRun.run_at.desc())))
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
        "running": pipeline.is_running(org),
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
