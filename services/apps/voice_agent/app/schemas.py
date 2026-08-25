"""Request models (camelCase) + serializers that emit the exact frontend shapes
(see frontend-voice-agent/lib/types.ts)."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel

from .models import Notification, Recording


# ---- request bodies (match frontend camelCase) ----
class ReplyDraftIn(BaseModel):
    body: str


class RelabelIn(BaseModel):
    label: str


class SettingsIn(BaseModel):
    sourceType: str = "bt_cloud"     # "bt_cloud" | "s3" — which fields below are in play
    # BT Cloud (RingCentral)
    endpoint: str = ""
    clientId: str = ""
    clientSecret: str = ""
    jwt: str = ""                    # RingCentral JWT credential (blank = keep stored)
    # S3-compatible bucket (AWS S3, Backblaze B2, MinIO, …)
    s3Endpoint: str = ""             # blank = AWS default; set for Backblaze B2/other
    s3Region: str = ""
    s3Bucket: str = ""
    s3Prefix: str = ""               # optional folder path within the bucket
    s3AccessKeyId: str = ""
    s3SecretAccessKey: str = ""      # blank = keep stored
    # shared
    cronFrequency: str = "every6h"  # hourly | every6h | daily
    cronTime: str = "02:00"
    enabled: bool = True


# ---- serializers (ORM -> frontend JSON) ----
def serialize_recording(r: Recording) -> dict:
    return {
        "id": r.id,
        "label": r.label,
        "caller": r.caller,
        "phone": r.phone,
        "agent": r.agent,
        "date": r.call_date,
        "duration": r.duration,
        "category": r.category,
        "priority": r.priority,
        "risk": r.risk,
        "sentiment": r.sentiment,
        "status": r.status,
        "needsReply": r.needs_reply,
        "replyStatus": r.reply_status,
        "summary": r.summary,
        "transcript": r.transcript,
        "aiReply": r.ai_reply,
        "analysisStatus": r.analysis_status,
        "audioAvailable": r.source_type == "s3",
    }


def _time_ago(dt: datetime | None) -> str:
    if not dt:
        return ""
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    mins = int((now - dt).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs} hour{'s' if hrs != 1 else ''} ago"
    days = hrs // 24
    return "Yesterday" if days == 1 else f"{days} days ago"


def serialize_notification(n: Notification) -> dict:
    return {
        "id": n.id,
        "text": n.text,
        "time": _time_ago(n.created_at),
        "read": n.read,
    }
