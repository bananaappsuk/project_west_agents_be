from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _id() -> str:
    return str(uuid.uuid4())


class Email(Base):
    __tablename__ = "emails"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    uid: Mapped[str] = mapped_column(String)  # IMAP UID (or Graph message id) — the dedup key
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
    # One code from the controlled PW taxonomy (agent_factory/app/taxonomy.py,
    # mirroring Reference 1 of the capability brief) — e.g. "new_referral",
    # "safeguarding_concern". Stored as the stable code, never the display label.
    category: Mapped[str] = mapped_column(String, default="")
    # Cross-cutting flags from the same taxonomy (urgent / sensitive /
    # safeguarding_indicator / court_deadline / uncertain). A separate axis from
    # `category`, not a refinement of it: a safeguarding disclosure inside a
    # complaint carries the flag while staying categorised as a complaint.
    flags: Mapped[list[str]] = mapped_column(
        ARRAY(String), default=list, server_default=text("'{}'::varchar[]")
    )
    # The queue this email was routed to (taxonomy.Queue — "referrals",
    # "safeguarding_restricted", "exception", ...). Derived from category+flags at
    # analysis time and stored, not recomputed on read, so that re-tuning the
    # routing table later cannot silently rewrite where past items went — the
    # audit record in brief §7.3 has to reflect what actually happened.
    route: Mapped[str] = mapped_column(String, default="", server_default="")
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
    # POST /intake/activity's own reference (e.g. "PW-A-2026-0009") — a separate
    # cross-referencing log entry sent for every email regardless of crm_status,
    # not to be confused with crm_reference (the referral/case outcome, if any).
    activity_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    # ── Project West mailbox identity and receiving-address evidence ──────────
    # THE authorization key. Every read filters on it; set at insert and never
    # written again (see the immutability trigger in migrate_pw_mailbox_model.sql).
    # A mutable authorization key is a vulnerability: anything able to update it
    # could move an email into a mailbox the attacker can read.
    mailbox_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # The business address the message actually arrived at. NEVER read this without
    # also reading receiving_address_status — on a non-resolved row its value is a
    # storage placeholder, not a determination.
    receiving_address: Mapped[str | None] = mapped_column(String, nullable=True)
    # The alias the sender typed, where one applies — what they *meant*, as opposed
    # to where the mail physically landed.
    original_recipient: Mapped[str | None] = mapped_column(String, nullable=True)
    #   resolved_single     — exactly one known business address matched
    #   resolved_precedence — several matched, resolved by the PW precedence rule
    #   ambiguous           — several matched, no rule available        -> EXCEPTION
    #   unresolved          — none matched (forward / DL / Bcc)         -> EXCEPTION
    #   legacy              — pre-PW row; recipient data was never collected at all
    # 'legacy' is deliberately distinct from 'unresolved': "we never looked" is not
    # "we looked and could not tell", and conflating them would misreport the
    # exception queue by every historical row.
    receiving_address_status: Mapped[str] = mapped_column(
        String, default="unresolved", server_default="unresolved"
    )
    # The raw recipient headers the decision was derived from, so a determination can
    # be audited or re-derived later without the original message. Without this a
    # wrong precedence rule is undetectable after the fact.
    recipient_evidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Thread matching. Graph conversationId.
    conversation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Stable cross-system identity and a second dedup signal. NOT unique: the same
    # message legitimately lands in two monitored mailboxes carrying an identical
    # value, and a unique constraint would silently drop the second copy.
    internet_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Safeguarding restriction. True when the receiving alias is restricted, the route
    # is safeguarding_restricted, or the classifier raised safeguarding_indicator —
    # the restricted route wins whenever EITHER fires.
    restricted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # buckets / lifecycle
    folder_id: Mapped[str | None] = mapped_column(String, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_by: Mapped[str | None] = mapped_column(String, nullable=True)  # cron | user
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("uid_validity", "uid", name="uq_email_uidvalidity_uid"),
        CheckConstraint(
            "receiving_address_status IN ('resolved_single','resolved_precedence',"
            "'ambiguous','unresolved','legacy')",
            name="ck_emails_receiving_address_status",
        ),
        Index("ix_emails_mailbox", "mailbox_id"),
        Index("ix_emails_conversation", "conversation_id"),
        Index("ix_emails_internet_msg_id", "internet_message_id"),
        Index("ix_emails_recv_status", "receiving_address_status"),
    )


class Folder(Base):
    __tablename__ = "folders"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String)
    color: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    high_priority: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[int] = mapped_column(Integer, default=0)  # prior emails swept to Archive this run
    status: Mapped[str] = mapped_column(String, default="success")  # running|success|partial|failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)  # set when status="failed"


class Mailbox(Base):
    """The monitored mailbox.

    Exactly one row today. The old `org_id` column carried a UNIQUE constraint, so
    "one mailbox" used to be a side effect of "one org"; with org_id gone, the
    singleton property is enforced by `ux_mailboxes_singleton` in
    migrate_drop_org_columns.sql instead of being lost silently.

    NOTE: the capability brief needs *six* monitored shared mailboxes (info,
    referrals, coordinator, finance, reports, management) plus alias routing onto
    them, which means this table becoming genuinely multi-row, each with its own
    sync watermark and reply identity. That is the next piece of work and is
    deliberately not half-built here — it is blocked on PW confirming the mailbox
    list and on Send As being verified beyond info@. The singleton index is what
    gets dropped when that lands.
    """
    __tablename__ = "mailboxes"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
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
    # truth for dedup — Email's (uid_validity, uid) unique constraint
    # guards against a duplicate insert even if this drifts.
    last_synced_uid: Mapped[str | None] = mapped_column(String, nullable=True)   # IMAP
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # Graph
    # The IMAP UIDVALIDITY last observed for this mailbox (see Email.uid_validity
    # for why this matters) — NULL until the first sync after this column was
    # added establishes a baseline. Not used for Graph mailboxes.
    uid_validity: Mapped[str | None] = mapped_column(String, nullable=True)
    # Bumped whenever Settings points this mailbox at a materially different
    # connection (see api.py's save_mailbox `identity_changed`). Folded into the
    # epoch string used for dedup scoping (pipeline.py's `current_uid_validity`)
    # so that a coincidentally-identical server-reported UIDVALIDITY across two
    # different mailbox generations (observed in practice: a provider does not
    # always bump UIDVALIDITY on what amounts to a full UID renumbering) can
    # never cause a new generation to be mistaken for a continuation of the old
    # one — without this, a genuinely new message landing on a UID number a
    # prior generation already used would be silently treated as a duplicate
    # and dropped instead of imported.
    sync_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # ── Project West system configuration ────────────────────────────────────
    # Seeded by migration, NOT user-editable. The Settings mailbox form is removed:
    # mailbox access is a property of the signed-in user's identity, not something an
    # administrator types into a form.
    #   'pw'     — an approved Project West shared mailbox, ingested via Graph
    #   'legacy' — pre-existing POC data, outside the PW ingestion path, never polled
    kind: Mapped[str] = mapped_column(String, default="pw", server_default="pw")
    visible: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    address: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Graph object id (ExternalDirectoryObjectId), known at seed time, so ingestion
    # needs no runtime directory lookup.
    graph_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Ingestion start boundary. NULL on a PW mailbox means "PW has not decided yet"
    # and the scheduler MUST refuse to sync it. This is the control that stops a
    # first sync walking 161,833 items in info@ — see decision 4.
    monitor_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Per-mailbox Graph watermark (receivedDateTime), replacing the IMAP-era
    # last_synced_uid/uid_validity pair.
    sync_cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Hard first-sync ceiling. A mis-set monitor_from must fail loudly and cheaply
    # rather than succeed expensively; the scheduler halts and alerts at this count.
    first_sync_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN ('pw','legacy')", name="ck_mailboxes_kind"),
        # A PW mailbox must carry everything ingestion needs. A legacy row needs none
        # of it, because nothing ever polls it.
        CheckConstraint(
            "kind <> 'pw' OR (address IS NOT NULL AND graph_user_id IS NOT NULL "
            "AND display_name IS NOT NULL)",
            name="ck_mailboxes_pw_complete",
        ),
    )


class MailboxAlias(Base):
    """A business entry point that routes into a physical mailbox.

    The mailbox is where the mail *is*; the alias is what the sender *meant*. Three
    of these collapse onto two mailboxes, so once a message is sitting in
    coordinator@ nothing in its content says whether it came via complaints@ or
    safeguarding@ — and those demand very different handling.

    System configuration, seeded by migration. Deliberately not exposed in the UI:
    brief Appendix B asks that routing be updatable "without major redevelopment",
    which a configuration row satisfies without putting alias routing in reach of an
    end user.
    """
    __tablename__ = "mailbox_aliases"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("mailboxes.id", ondelete="CASCADE"))
    address: Mapped[str] = mapped_column(String, unique=True)
    # Taxonomy queue this entry point implies. "" = no override.
    queue: Mapped[str] = mapped_column(String, default="", server_default="")
    reply_allowed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    restricted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # False for tenant routing addresses (*.onmicrosoft.com): real delivery targets
    # carrying no business meaning, catalogued so mail arriving at one RESOLVES
    # instead of falling into the exception path.
    business: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MailboxAccessRule(Base):
    """Entra GROUP -> mailbox grant. Never bound to an individual user.

    The brief forbids hard-coding individual Project West people. PW IT adds someone
    to a group in Entra and their access changes at next sign-in — no deployment, no
    application change. This table stores which *group* grants which mailbox (~20
    stable rows) and never stores who is in it, which also keeps joiner/leaver
    handling where it belongs.
    """
    __tablename__ = "mailbox_access_rule"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    entra_group_id: Mapped[str] = mapped_column(String)
    entra_group_name: Mapped[str | None] = mapped_column(String, nullable=True)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("mailboxes.id", ondelete="CASCADE"))
    can_read: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    can_send: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    can_approve: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("entra_group_id", "mailbox_id", name="uq_mailbox_access_rule"),
    )


class UserMailboxAccess(Base):
    """The resolved authorised set for one user — a materialised view of the rules.

    Lives in the database rather than the JWT deliberately: a claim would be faster
    and equally tamper-proof, but revocation would lag by the access-token lifetime,
    and for safeguarding material that lag is not acceptable.
    """
    __tablename__ = "user_mailbox_access"
    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    mailbox_id: Mapped[str] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="CASCADE"), primary_key=True
    )
    can_read: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    can_send: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    can_approve: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SafeguardingClearance(Base):
    """A separate axis from mailbox access, in its own table because this grant's
    history is the one most likely to be audited.

    It has to be separate: safeguarding@ is an alias into coordinator@, and every
    named user holds Full Access on coordinator@. If clearance were a mailbox
    permission it would already be granted to everyone.
    """
    __tablename__ = "safeguarding_clearance"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    user_id: Mapped[str] = mapped_column(String)
    granted_by: Mapped[str | None] = mapped_column(String, nullable=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (Index("ix_safeguarding_clearance_user", "user_id"),)


class EmailAudit(Base):
    """Append-only per-action audit trail.

    Run-level counters (AgentRun) are not sufficient: the SSOT requires the send
    record to capture the approving user, the identity actually used, and the alias
    decision. No UPDATE or DELETE path exists for this table in the application.
    """
    __tablename__ = "email_audit"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    email_id: Mapped[str] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"))
    mailbox_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # ingested | classified | routed | drafted | approved | rejected | sent
    # | send_failed | reclassified | restricted_applied
    action: Mapped[str] = mapped_column(String)
    actor_user_id: Mapped[str | None] = mapped_column(String, nullable=True)  # NULL for agent
    actor_kind: Mapped[str] = mapped_column(String, default="system", server_default="system")
    # The From identity actually used on a send, and whether an alias was chosen.
    sender_identity: Mapped[str | None] = mapped_column(String, nullable=True)
    alias_decision: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        Index("ix_email_audit_email", "email_id", "created_at"),
        Index("ix_email_audit_actor", "actor_user_id", "created_at"),
        Index("ix_email_audit_action", "action", "created_at"),
    )


class AgentConfig(Base):
    """Agent settings. One row, enforced by `ux_agent_config_singleton` — see
    Mailbox above for why that index exists now that org_id is gone."""
    __tablename__ = "agent_config"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    cron_interval: Mapped[str] = mapped_column(String, default="Every hour")
    # No longer drives how much a sync fetches — that's now the mailbox's sync
    # watermark (Mailbox.last_synced_uid/at), which always fetches everything
    # not-yet-synced rather than a fixed per-run count. Kept as a stored/exposed
    # field for now since nothing reads it as a behavioral cap anymore.
    fetch_per_run: Mapped[int] = mapped_column(Integer, default=20)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Org opt-in: off by default — nothing auto-sends until an admin turns this on.
    auto_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
