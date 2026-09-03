from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _id() -> str:
    return str(uuid.uuid4())


class Recording(Base):
    __tablename__ = "recordings"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    org_id: Mapped[str] = mapped_column(String, index=True)
    ext_id: Mapped[str] = mapped_column(String)  # BT Cloud recording id / S3 object key — dedup key within an org
    # Which connector this specific recording came from — "bt_cloud" | "s3". Recorded
    # at fetch time (not read from the org's *current* settings) so playback knows
    # whether real audio is even fetchable, regardless of what the org is on now.
    source_type: Mapped[str] = mapped_column(String, default="bt_cloud")
    # User-facing display name, distinct from `caller` (a real call's caller ID is
    # not something the user should be renaming). Set at upload time (defaults to
    # the file name minus its extension) and editable afterward from the detail
    # page. Falls back to `caller` in the UI when unset — see serialize_recording.
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    caller: Mapped[str] = mapped_column(String, default="")
    phone: Mapped[str] = mapped_column(String, default="")
    agent: Mapped[str] = mapped_column(String, default="")
    call_date: Mapped[str] = mapped_column(String, default="")   # YYYY-MM-DD (frontend renders as-is)
    duration: Mapped[str] = mapped_column(String, default="")    # "M:SS"
    transcript: Mapped[str] = mapped_column(Text, default="")
    # BT Cloud recording content URL, captured at fetch time — lets GET /recordings/{id}/audio
    # re-fetch the audio on demand (with a fresh/cached token) without ever storing the bytes.
    # Null for S3/upload sources (their `ext_id` already doubles as the storage key) and for
    # recordings synced before this column existed.
    content_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    # AI fields (filled by the Agent Factory voice agent)
    summary: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String, default="General Enquiry")
    priority: Mapped[str] = mapped_column(String, default="Medium")     # High | Medium | Low
    risk: Mapped[str] = mapped_column(String, default="Low")            # High | Medium | Low
    sentiment: Mapped[str] = mapped_column(String, default="Neutral")   # Positive | Neutral | Negative
    needs_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_reply: Mapped[str] = mapped_column(Text, default="")
    reply_status: Mapped[str] = mapped_column(String, default="none")   # none|pending|edited|approved|sent|rejected
    analysis_status: Mapped[str] = mapped_column(String, default="pending")  # pending|done|failed
    # CRM intake (pw-crm-be) outcome for this call's detected intent, if any.
    crm_status: Mapped[str] = mapped_column(String, default="none")        # none|sent|skipped|failed
    crm_reference: Mapped[str | None] = mapped_column(String, nullable=True)  # PW-R-... referral ref, or the case_ref
    # POST /intake/activity's own reference (e.g. "PW-A-2026-0009") — a separate
    # cross-referencing log entry sent for every call regardless of crm_status,
    # not to be confused with crm_reference (the referral/case outcome, if any).
    activity_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    # new = latest cron extraction; old = swept by a later run
    status: Mapped[str] = mapped_column(String, default="new", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("org_id", "ext_id", name="uq_recording_org_ext"),)


class VoiceSettings(Base):
    __tablename__ = "voice_settings"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    org_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    # Which connector fetch_and_process() dispatches to — "bt_cloud" | "s3". Both field sets
    # below persist regardless of which is active, so switching doesn't discard the other.
    source_type: Mapped[str] = mapped_column(String, default="bt_cloud")
    # BT Cloud (RingCentral)
    endpoint: Mapped[str] = mapped_column(String, default="")  # RingCentral/BT Cloud Work server URL
    client_id: Mapped[str] = mapped_column(String, default="")
    client_secret_enc: Mapped[str] = mapped_column(Text, default="")
    jwt_enc: Mapped[str] = mapped_column(Text, default="")  # RingCentral JWT credential (server-to-server auth)
    # S3-compatible bucket (AWS S3, Backblaze B2, MinIO, …)
    s3_endpoint: Mapped[str] = mapped_column(String, default="")  # blank = AWS default; set for Backblaze B2/other
    s3_region: Mapped[str] = mapped_column(String, default="")
    s3_bucket: Mapped[str] = mapped_column(String, default="")
    s3_prefix: Mapped[str] = mapped_column(String, default="")  # optional folder path within the bucket
    s3_access_key_id: Mapped[str] = mapped_column(String, default="")
    s3_secret_access_key_enc: Mapped[str] = mapped_column(Text, default="")
    # shared
    cron_frequency: Mapped[str] = mapped_column(String, default="every6h")  # hourly|every6h|daily
    cron_time: Mapped[str] = mapped_column(String, default="02:00")         # HH:mm (daily only)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    __tablename__ = "voice_notifications"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    org_id: Mapped[str] = mapped_column(String, index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentRun(Base):
    __tablename__ = "voice_agent_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    org_id: Mapped[str] = mapped_column(String, index=True)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    high_risk: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="success")  # running|success|partial|failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)  # set when status="failed"
