-- Project West mailbox / authorization model.
--
-- No migration tool in this project (no Alembic) — schema is normally created via
-- SQLAlchemy create_all(), which only creates missing TABLES and never alters an
-- existing one. Apply by hand. Written to be safe to run more than once.
--
-- ORDER: this runs AFTER migrate_drop_org_columns.sql and
-- migrate_add_taxonomy_columns.sql. It assumes `org_id` is gone and that
-- `ux_mailboxes_singleton` exists. It will fail loudly rather than half-apply if not
-- (see the preflight block).
--
-- SSOT: MAILBOX_AUTHORIZATION_PLAN.md §6, corrected by live tenant verification —
-- SIX physical shared mailboxes and FOUR business aliases, not seven and three.
-- `admin@` is an alias of `management@`; it is NOT a seventh mailbox.
--
-- This migration is ADDITIVE ONLY. It drops no data. The existing POC mailbox row and
-- all 31,809 existing `emails` rows are preserved and are assigned to a `legacy`
-- mailbox that is invisible in the UI and outside the Project West ingestion path.

BEGIN;

-- ── preflight ─────────────────────────────────────────────────────────────────
-- Fail before changing anything if the prerequisite migrations have not run.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'emails'
                  AND column_name = 'org_id') THEN
        RAISE EXCEPTION
            'PREREQUISITE MISSING: emails.org_id still exists. Run migrate_drop_org_columns.sql first.';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'emails'
                      AND column_name = 'route') THEN
        RAISE EXCEPTION
            'PREREQUISITE MISSING: emails.route does not exist. Run migrate_add_taxonomy_columns.sql first.';
    END IF;
END $$;

-- ── mailboxes: from "one connection an admin configured" to system configuration ──
-- The table is extended rather than replaced, so the existing POC row survives and
-- keeps its id (every one of the 31,809 emails will point at it as `legacy`).
ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS address        varchar;
ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS display_name   varchar;
ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS graph_user_id  varchar;
-- Ingestion start boundary. NULL on a PW mailbox means "not yet decided" and the
-- scheduler must refuse to sync it — see the CHECK below. This is the control that
-- stops a first sync walking 161,833 items in info@.
ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS monitor_from   timestamptz;
-- Per-mailbox Graph watermark (receivedDateTime), replacing the IMAP-era
-- last_synced_uid/uid_validity pair for PW mailboxes.
ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS sync_cursor    timestamptz;
-- 'pw'      — an approved Project West shared mailbox, ingested via Graph
-- 'legacy'  — pre-existing POC data, outside the PW ingestion path, never polled
ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS kind           varchar NOT NULL DEFAULT 'pw';
-- Invisible in the mailbox switcher. The SSOT's "seeded but not visible" legacy row.
ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS visible        boolean NOT NULL DEFAULT true;
-- Per-mailbox first-sync ceiling. A mis-set monitor_from must fail loudly and cheaply
-- rather than succeed expensively; the scheduler halts and alerts at this count.
ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS first_sync_cap integer;

ALTER TABLE mailboxes DROP CONSTRAINT IF EXISTS ck_mailboxes_kind;
ALTER TABLE mailboxes ADD  CONSTRAINT ck_mailboxes_kind
    CHECK (kind IN ('pw', 'legacy'));

-- A PW mailbox must carry the three things ingestion needs. A legacy row needs none
-- of them, because nothing ever polls it.
ALTER TABLE mailboxes DROP CONSTRAINT IF EXISTS ck_mailboxes_pw_complete;
ALTER TABLE mailboxes ADD  CONSTRAINT ck_mailboxes_pw_complete
    CHECK (
        kind <> 'pw'
        OR (address IS NOT NULL AND graph_user_id IS NOT NULL AND display_name IS NOT NULL)
    );

-- Deliberately NOT a partial index. `ON CONFLICT (address)` in the seed migration
-- cannot infer a partial unique index without repeating its predicate, and Postgres
-- already treats NULLs as distinct in a unique index — so a plain one gives the same
-- "many rows may have no address" behaviour while staying inferrable.
CREATE UNIQUE INDEX IF NOT EXISTS ux_mailboxes_address ON mailboxes (address);

-- The whole point of this work: more than one mailbox.
DROP INDEX IF EXISTS ux_mailboxes_singleton;

-- ── the existing POC row becomes `legacy` ─────────────────────────────────────
-- Gmail-derived history is pre-existing POC data, not Project West mail. It is kept,
-- hidden, and never polled. Nothing is deleted and nothing is migrated into the PW
-- workflow.
UPDATE mailboxes
   SET kind         = 'legacy',
       visible      = false,
       enabled      = false,
       display_name = COALESCE(display_name, 'Legacy (pre-PW POC data)'),
       address      = COALESCE(address, username)
 WHERE kind <> 'legacy'
   AND (provider <> 'graph' OR graph_user_id IS NULL);

-- ── business aliases ──────────────────────────────────────────────────────────
-- An alias is what the sender MEANT; the mailbox is where the mail IS.
CREATE TABLE IF NOT EXISTS mailbox_aliases (
    id            varchar PRIMARY KEY,
    mailbox_id    varchar NOT NULL REFERENCES mailboxes (id) ON DELETE CASCADE,
    address       varchar NOT NULL UNIQUE,
    -- Taxonomy queue this entry point implies. Empty string = no queue override.
    queue         varchar NOT NULL DEFAULT '',
    reply_allowed boolean NOT NULL DEFAULT false,
    restricted    boolean NOT NULL DEFAULT false,
    -- false for tenant routing addresses (*.onmicrosoft.com): real delivery targets
    -- that carry no business meaning, catalogued so they resolve instead of landing
    -- in the exception path.
    business      boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_mailbox_aliases_mailbox ON mailbox_aliases (mailbox_id);

-- ── authorization: rules bind to Entra GROUPS, never to individual users ──────
CREATE TABLE IF NOT EXISTS mailbox_access_rule (
    id               varchar PRIMARY KEY,
    entra_group_id   varchar NOT NULL,
    entra_group_name varchar,
    mailbox_id       varchar NOT NULL REFERENCES mailboxes (id) ON DELETE CASCADE,
    can_read         boolean NOT NULL DEFAULT false,
    can_send         boolean NOT NULL DEFAULT false,
    can_approve      boolean NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_mailbox_access_rule UNIQUE (entra_group_id, mailbox_id)
);

-- Resolved per user at sign-in: a materialised view of the rules above.
CREATE TABLE IF NOT EXISTS user_mailbox_access (
    user_id     varchar NOT NULL,
    mailbox_id  varchar NOT NULL REFERENCES mailboxes (id) ON DELETE CASCADE,
    can_read    boolean NOT NULL DEFAULT false,
    can_send    boolean NOT NULL DEFAULT false,
    can_approve boolean NOT NULL DEFAULT false,
    resolved_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, mailbox_id)
);
CREATE INDEX IF NOT EXISTS ix_user_mailbox_access_user ON user_mailbox_access (user_id);

-- Its own table, not a column: this grant's history is the one most likely to be
-- audited. Clearance is a SEPARATE axis from mailbox access — safeguarding@ is an
-- alias into coordinator@, which every named user can already read.
CREATE TABLE IF NOT EXISTS safeguarding_clearance (
    id         varchar PRIMARY KEY,
    user_id    varchar NOT NULL,
    granted_by varchar,
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);
CREATE INDEX IF NOT EXISTS ix_safeguarding_clearance_user
    ON safeguarding_clearance (user_id) WHERE revoked_at IS NULL;

-- ── emails: mailbox identity + receiving-address evidence ────────────────────
-- Added NULLABLE. A later migration sets NOT NULL once every row has a value.
ALTER TABLE emails ADD COLUMN IF NOT EXISTS mailbox_id          varchar REFERENCES mailboxes (id);
ALTER TABLE emails ADD COLUMN IF NOT EXISTS receiving_address   varchar;
ALTER TABLE emails ADD COLUMN IF NOT EXISTS original_recipient  varchar;
ALTER TABLE emails ADD COLUMN IF NOT EXISTS conversation_id     varchar;
ALTER TABLE emails ADD COLUMN IF NOT EXISTS internet_message_id varchar;

-- The load-bearing signal. Nothing may read receiving_address without reading this.
--   resolved_single     — exactly one known business address matched
--   resolved_precedence — several matched, resolved by the PW precedence rule
--   ambiguous           — several matched, no rule available        → EXCEPTION
--   unresolved          — none matched (forward / DL / Bcc)         → EXCEPTION
--   legacy              — pre-PW row; never had recipient data collected
ALTER TABLE emails ADD COLUMN IF NOT EXISTS receiving_address_status varchar
    NOT NULL DEFAULT 'unresolved';

-- The raw recipient headers the decision was derived from, so a receiving-address
-- determination can be audited or re-derived later without the original message.
-- Without this, a wrong precedence rule is undetectable after the fact.
ALTER TABLE emails ADD COLUMN IF NOT EXISTS recipient_evidence jsonb;

-- Safeguarding restriction, set at ingestion. True when the receiving alias is
-- restricted, the route is safeguarding_restricted, or the classifier flagged
-- safeguarding_indicator. The restricted route wins whenever EITHER fires.
ALTER TABLE emails ADD COLUMN IF NOT EXISTS restricted boolean NOT NULL DEFAULT false;

ALTER TABLE emails DROP CONSTRAINT IF EXISTS ck_emails_receiving_address_status;
ALTER TABLE emails ADD  CONSTRAINT ck_emails_receiving_address_status
    CHECK (receiving_address_status IN
           ('resolved_single', 'resolved_precedence', 'ambiguous', 'unresolved', 'legacy'));

CREATE INDEX IF NOT EXISTS ix_emails_mailbox         ON emails (mailbox_id);
CREATE INDEX IF NOT EXISTS ix_emails_conversation    ON emails (conversation_id);
-- NOT unique, deliberately: the same message legitimately lands in two monitored
-- mailboxes with an identical internetMessageId. A unique constraint would silently
-- drop the second copy. It is a matching signal, not an identity.
CREATE INDEX IF NOT EXISTS ix_emails_internet_msg_id ON emails (internet_message_id);
CREATE INDEX IF NOT EXISTS ix_emails_restricted      ON emails (restricted) WHERE restricted;
CREATE INDEX IF NOT EXISTS ix_emails_recv_status     ON emails (receiving_address_status);

-- ── audit trail ───────────────────────────────────────────────────────────────
-- Run-level counters (agent_runs) are not sufficient. SSOT §12 requires the send
-- record to capture the approving user, the identity actually used, and the alias
-- decision. Append-only; no UPDATE or DELETE path exists in the application.
CREATE TABLE IF NOT EXISTS email_audit (
    id              varchar PRIMARY KEY,
    email_id        varchar NOT NULL REFERENCES emails (id) ON DELETE CASCADE,
    mailbox_id      varchar REFERENCES mailboxes (id),
    -- ingested | classified | routed | drafted | approved | rejected | sent
    -- | send_failed | reclassified | restricted_applied
    action          varchar NOT NULL,
    actor_user_id   varchar,          -- NULL for agent/system actions
    actor_kind      varchar NOT NULL DEFAULT 'system',  -- system | user
    -- The From identity actually used on a send, and whether it was an alias.
    sender_identity varchar,
    alias_decision  varchar,
    detail          jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_email_audit_email  ON email_audit (email_id, created_at);
CREATE INDEX IF NOT EXISTS ix_email_audit_actor  ON email_audit (actor_user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_email_audit_action ON email_audit (action, created_at);

-- ── backfill: existing rows become legacy, explicitly ────────────────────────
-- Every existing row came from the single configured POC mailbox. It is associated
-- with that mailbox (now kind='legacy') and marked 'legacy' for receiving address —
-- NOT 'unresolved', because these rows never had recipient data collected at all.
-- That distinction matters: 'unresolved' means "we looked and could not tell";
-- 'legacy' means "we never looked". Conflating them would misreport the exception
-- queue by 31,809 rows.
UPDATE emails e
   SET mailbox_id               = (SELECT m.id FROM mailboxes m WHERE m.kind = 'legacy' LIMIT 1),
       receiving_address_status = 'legacy'
 WHERE e.mailbox_id IS NULL;

COMMIT;
