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


class Email(Base):
    __tablename__ = "emails"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    org_id: Mapped[str] = mapped_column(String, index=True)
    uid: Mapped[str] = mapped_column(String)  # IMAP UID (or Graph message id) — dedup key within an org
    # The IMAP UIDVALIDITY in effect when `uid` was assigned, or the fixed
    # sentinel "graph" for Graph-provider rows (Graph ids are already globally
    # stable, no reset concept). Part of the dedup identity alongside `uid`
    # (see __table_args__ below) — if a mailbox's UIDVALIDITY ever resets (a
    # real IMAP event: mailbox rebuild/migration), UID numbers start over from
    # the server's perspective, so a genuinely new message can be assigned a UID
    # a prior email already used. Without this column that collided with the
    # dedup check and got silently dropped as "already synced"; with it, the two
    # eras never compare equal. NULL means "pre-dates this column" (migrated-in
    # legacy rows) — see pipeline.py's _known_uids for how those are handled.
    uid_validity: Mapped[str | None] = mapped_column(String, nullable=True)
    sender: Mapped[str] = mapped_column(String)         # display name -> JSON "from"
    from_email: Mapped[str] = mapped_column(String)     # -> "fromEmail"
    subject: Mapped[str] = mapped_column(String, default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    body: Mapped[str] = mapped_column(Text, default="")
    content_type: Mapped[str] = mapped_column(String, default="text")  # "text" | "html"
    # AI fields
    summary: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String, default="")
    priority: Mapped[str] = mapped_column(String, default="Medium")  # High | Medium | Low
    confidence: Mapped[float] = mapped_column(default=0.0)
    needs_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    draft_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_status: Mapped[str] = mapped_column(String, default="pending")  # pending|done|failed
    reply_status: Mapped[str] = mapped_column(String, default="none")       # none|draft|sent|rejected
    auto_sent: Mapped[bool] = mapped_column(Boolean, default=False)         # sent by the AI, not a human
    # CRM intake (pw-crm-be) outcome for this email's detected intent, if any.
    intent: Mapped[str] = mapped_column(String, default="NONE")  # REFERRAL|CASE_COMMUNICATION|RESCHEDULE|CANCEL|NONE
    crm_status: Mapped[str] = mapped_column(String, default="none")        # none|sent|skipped|failed
    crm_reference: Mapped[str | None] = mapped_column(String, nullable=True)  # PW-R-... referral ref, or the case_ref
    # buckets / lifecycle
    folder_id: Mapped[str | None] = mapped_column(String, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_by: Mapped[str | None] = mapped_column(String, nullable=True)  # cron | user
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("org_id", "uid_validity", "uid", name="uq_email_org_uidvalidity_uid"),)


class Folder(Base):
    __tablename__ = "folders"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    org_id: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String)
    color: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    org_id: Mapped[str] = mapped_column(String, index=True)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    high_priority: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[int] = mapped_column(Integer, default=0)  # prior emails swept to Archive this run
    status: Mapped[str] = mapped_column(String, default="success")  # running|success|partial|failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)  # set when status="failed"


class Mailbox(Base):
    __tablename__ = "mailboxes"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    org_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    imap_host: Mapped[str] = mapped_column(String)
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    smtp_host: Mapped[str] = mapped_column(String)
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    username: Mapped[str] = mapped_column(String)
    password_enc: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Reading (and sending) via Microsoft Graph (app-only), for tenants with IMAP
    # Basic Auth disabled — covers both directions, so smtp_host/smtp_port/
    # password_enc above go unused for "graph" rows (kept, not nulled, since
    # there's no migration path to make them nullable — see graph_client.py).
    provider: Mapped[str] = mapped_column(String, default="imap", server_default="imap")  # "imap" | "graph"
    tenant_id: Mapped[str | None] = mapped_column(String, nullable=True)
    client_id: Mapped[str | None] = mapped_column(String, nullable=True)
    client_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Sync watermark — "how far we've gotten" so a normal sync only asks the
    # mailbox for what's new instead of re-scanning the whole thing every time.
    # NULL means "never synced" — the *first* sync for a mailbox therefore
    # naturally pulls everything, however large the backlog. Purely a scope
    # optimization for the SEARCH/query the provider runs, never the source of
    # truth for dedup — Email's (org_id, uid_validity, uid) unique constraint
    # guards against a duplicate insert even if this drifts.
    last_synced_uid: Mapped[str | None] = mapped_column(String, nullable=True)   # IMAP
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # Graph
    # The IMAP UIDVALIDITY last observed for this mailbox (see Email.uid_validity
    # for why this matters) — NULL until the first sync after this column was
    # added establishes a baseline. Not used for Graph mailboxes.
    uid_validity: Mapped[str | None] = mapped_column(String, nullable=True)


class AgentConfig(Base):
    __tablename__ = "agent_config"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    org_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    cron_interval: Mapped[str] = mapped_column(String, default="Every hour")
    # No longer drives how much a sync fetches — that's now the mailbox's sync
    # watermark (Mailbox.last_synced_uid/at), which always fetches everything
    # not-yet-synced rather than a fixed per-run count. Kept as a stored/exposed
    # field for now since nothing reads it as a behavioral cap anymore.
    fetch_per_run: Mapped[int] = mapped_column(Integer, default=20)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Org opt-in: off by default — nothing auto-sends until an admin turns this on.
    auto_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
