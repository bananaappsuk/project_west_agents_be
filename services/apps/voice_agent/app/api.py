from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import bt_client, crypto, pipeline, s3_client
from .billing_client import BillingBlocked
from .config import settings
from .db import get_session
from .deps import require
from .models import AgentRun, Notification, Recording, VoiceSettings
from .schemas import (
    ReplyDraftIn,
    SettingsIn,
    serialize_notification,
    serialize_recording,
)

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


@router.post("/recordings/fetch")
async def fetch_recordings(
    count: int = 12,
    sweep: bool = True,
    claims: dict = Depends(require(WRITE)),
    session: AsyncSession = Depends(get_session),
):
    try:
        new_ids = await pipeline.fetch_and_process(_org(claims), count, sweep=sweep)
    except BillingBlocked as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, {"code": exc.code, "message": str(exc)}) from exc
    except LookupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"BT Cloud fetch failed: {exc}") from exc
    if not new_ids:
        return []
    rows = await session.scalars(select(Recording).where(Recording.id.in_(new_ids)))
    return [serialize_recording(r) for r in rows]


@router.get("/recordings/{rec_id}")
async def get_recording(rec_id: str, claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    return serialize_recording(await _get_recording(session, _org(claims), rec_id))


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
            "totalHighRisk": sum(r.high_risk for r in runs),
        },
        "history": [
            {"runAt": r.run_at.isoformat(), "fetched": r.fetched, "processed": r.processed,
             "highRisk": r.high_risk, "status": r.status}
            for r in runs[:20]
        ],
        "daily": sorted(daily.values(), key=lambda d: d["day"])[-7:],
    }
