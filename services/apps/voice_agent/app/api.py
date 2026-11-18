from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta

import hmac

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import agent_client, bt_client, crm_sync, pipeline, s3_client, source_config, transcribe
from .config import settings
from .db import get_session
from .deps import require
from .models import AgentRun, Notification, Recording
from .schemas import (
    RelabelIn,
    ReplyDraftIn,
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
    # Checks that credentials are actually PRESENT, not merely that a row exists.
    # The old guard passed whenever a voice_settings row was enabled, and the
    # settings reader created one the moment anyone loaded the dashboard - so on a
    # fresh database this route fell through to BT Cloud's demo fallback and filled
    # it with fabricated recordings indistinguishable from real ones once stored.
    src = source_config.load()
    if not src.configured:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, src.reason)

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
        src = source_config.load()
        if src.source_type != source_config.S3 or not src.configured:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "saving audio needs S3-compatible storage configured in the "
                "environment (VOICE_SOURCE_TYPE=s3 with S3_BUCKET and keys)",
            )
        ext = "wav" if "wav" in content_type else "mp3"
        key = f"uploads/{ext_id}.{ext}"
        try:
            await run_in_threadpool(
                s3_client.upload_object,
                key=key, body=audio, content_type=content_type,
                **src.transcribe_creds,
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


@router.post("/crm-callback", status_code=status.HTTP_204_NO_CONTENT)
async def crm_callback(
    body: dict,
    x_callback_secret: str = Header(default=""),
    session: AsyncSession = Depends(get_session),
):
    """The CRM telling us what a coordinator decided about one submission.

    Since the CRM stopped creating records on submission, "we sent it" and
    "it exists" are different facts, and only the CRM knows when the second
    becomes true. Without this the dashboard would show every call as
    awaiting approval for ever.

    Not behind the normal bearer auth: the caller is a server, not a signed-in
    user. It proves itself with a shared secret, compared with
    `hmac.compare_digest` so the comparison cannot be timed. An unset secret
    refuses everything rather than waving callers through -- this endpoint can
    change what the CRM state of a call says, so an unconfigured deployment
    must fail closed.
    """
    expected = settings.crm_callback_secret
    if not expected or not hmac.compare_digest(x_callback_secret, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad callback secret")

    ext_id = (body.get("external_ref") or "").strip()
    if not ext_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "external_ref is required")

    r = await session.scalar(select(Recording).where(Recording.ext_id == ext_id))
    if not r:
        # Not an error worth retrying: the CRM holds submissions from both
        # agents and only one of them owns any given ext_id.
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    review_status = (body.get("review_status") or "").upper()
    if review_status == "APPLIED":
        r.crm_status = "sent"
        r.crm_reference = body.get("applied_reference") or r.crm_reference
    elif review_status == "REJECTED":
        r.crm_status = "rejected"
    elif review_status in ("APPLY_FAILED",):
        r.crm_status = "failed"
    else:
        r.crm_status = "pending_approval"

    await session.commit()
    log.info("crm callback ext_id=%s -> %s", ext_id, r.crm_status)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/recordings/by-ext/{ext_id:path}")
async def get_recording_by_ext(
    ext_id: str,
    claims: dict = Depends(require(READ)),
    session: AsyncSession = Depends(get_session),
):
    """Find a call by the id its source gave it, rather than ours.

    The CRM only ever learns a recording's `ext_id` -- that is what travels on
    every intake submission as `external_ref`. Our own uuid never leaves this
    service, so without this a coordinator reviewing an agent's summary had no
    way back to the call it came from.

    Declared above GET /recordings/{rec_id}: that route would otherwise match
    "by-ext" as a recording id and 404 every link.

    `:path` because a BT Cloud ext_id can contain characters that would
    otherwise end the segment.
    """
    r = await session.scalar(select(Recording).where(Recording.ext_id == ext_id))
    if not r:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "recording not found")
    return serialize_recording(r)


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


async def _fetch_recording_audio(r: Recording) -> tuple[bytes, str]:
    """Re-fetches a recording's audio from its source on demand — nothing is stored
    locally (see pipeline.py: audio is discarded right after transcription). S3-sourced
    recordings are re-fetched from the bucket; BT Cloud recordings are re-fetched from
    RingCentral using a fresh/cached access token (see bt_client.fetch_audio) — only
    possible for recordings synced after content_uri started being captured, with a JWT
    still configured. Shared by GET .../audio (playback) and
    POST .../retry-analysis (re-transcribing a recording whose transcript never came
    through).

    Note it keys off the RECORDING's `source_type`, not the currently configured one:
    a row captured from a source the deployment has since moved away from has no
    retrievable audio, and must 404 rather than be fetched with the wrong credentials.
    """
    src = source_config.load()
    if r.source_type != src.source_type or not src.configured:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "audio not available for this recording")

    if r.source_type == source_config.S3:
        try:
            return await run_in_threadpool(s3_client.download, key=r.ext_id, **src.transcribe_creds)
        except Exception as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"could not fetch audio: {exc}") from exc

    if r.source_type == source_config.BT_CLOUD:
        if not r.content_uri:
            # Captured before content_uri was recorded: there is no address to
            # re-fetch from, and no credential makes that retrievable.
            raise HTTPException(status.HTTP_404_NOT_FOUND, "audio not available for this recording")
        try:
            return await run_in_threadpool(
                bt_client.fetch_audio, content_uri=r.content_uri, **src.creds
            )
        except Exception as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"could not fetch audio: {exc}") from exc

    raise HTTPException(status.HTTP_404_NOT_FOUND, "audio not available for this recording")


@router.get("/recordings/{rec_id}/audio")
async def get_recording_audio(rec_id: str, claims: dict = Depends(require(READ)), session: AsyncSession = Depends(get_session)):
    r = await _get_recording(session, rec_id)
    audio, content_type = await _fetch_recording_audio(r)
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
        transcript = r.transcript
        if not transcript or transcript == "(no transcript available)":
            audio, content_type = await _fetch_recording_audio(r)
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
    src = source_config.load()

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
        # Healthy means the agent can actually reach a source, not merely that a
        # settings row exists. An unconfigured deployment reports Down.
        "syncStatus": "Healthy" if (settings.cron_enabled and src.configured) else "Down",
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


# ---------- recording source (read-only; configured in the environment) ----------
@router.get("/settings")
async def get_settings(claims: dict = Depends(require(READ))):
    """What recording source this deployment is pointed at, and whether it is usable.

    Read-only. The source is configured from environment variables and does not
    change at runtime (see source_config.py), so there is nothing here to write.
    Kept as a route because "is the JWT actually set on this environment?" is the
    first question asked when a sync produces nothing, and answering it from the
    service itself beats reading someone's deployment dashboard over their shoulder.

    Secrets are reported as booleans and never returned, not even partially.
    """
    return source_config.public_view()


# `POST /settings` and `POST /settings/test` are deliberately gone. They wrote
# credentials into the voice_settings row, which is no longer where the source is
# read from -- so keeping them would have let someone save a configuration that the
# running service then ignored. Connection problems now surface against the real
# deployed configuration: GET /settings reports whether it is complete, and
# POST /recordings/fetch refuses with the same reason when it is not.


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
    src = source_config.load()
    enabled = settings.cron_enabled and src.configured
    runs = list(await session.scalars(select(AgentRun).order_by(AgentRun.run_at.desc())))
    last = runs[0] if runs else None

    daily: dict[str, dict] = {}
    for r in runs:
        day = r.run_at.date().isoformat()
        d = daily.setdefault(day, {"day": day, "processed": 0, "highRisk": 0})
        d["processed"] += r.processed
        d["highRisk"] += r.high_risk

    next_run = ""
    if enabled and last:
        interval = _FREQ_MINUTES.get(src.cron_frequency, 360)
        next_run = (last.run_at + timedelta(minutes=interval)).isoformat()

    return {
        "running": pipeline.is_running(),
        "config": {
            "cronFrequency": src.cron_frequency,
            "cronTime": src.cron_time,
            "enabled": enabled,
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
