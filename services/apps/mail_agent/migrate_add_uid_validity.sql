-- This project has no migration tool (no Alembic) — schema is normally created via
-- SQLAlchemy's create_all(), which only creates missing tables and never alters an
-- existing one. The `mailboxes` and `emails` tables already exist, so the new
-- uid_validity columns (and the dedup unique constraint change on `emails`) must be
-- applied by hand with this script. Safe to run multiple times.
--
-- Why: Email.uid alone (IMAP UID) is not a permanently stable identity — if a
-- mailbox's IMAP UIDVALIDITY ever resets (a real server-side event: mailbox
-- rebuild/migration), UID numbers start over, and a genuinely new message can be
-- assigned a UID a prior email already used for this org. Before this migration,
-- that collided with the (org_id, uid) unique constraint's dedup check and the new
-- message was silently treated as "already synced" and never imported — permanent,
-- silent data loss with no error logged. Scoping the dedup identity to
-- (org_id, uid_validity, uid) instead means two different UID eras never compare
-- equal. See models.py's Email.uid_validity / Mailbox.uid_validity for the full
-- explanation, and pipeline.py's _known_uids/_run for how a reset is detected and
-- handled going forward.
--
-- Existing rows are intentionally left with uid_validity = NULL (no live IMAP
-- connection to backfill their real historic UIDVALIDITY from this script, and
-- guessing wrong would be worse than leaving it unknown) — pipeline.py's dedup
-- query treats NULL as "legacy, always considered known" so this does not cause
-- re-importing/duplicating any already-synced mail. Each mailbox picks up its real
-- current UIDVALIDITY as a baseline on its next sync after this migration runs, with
-- no forced rescan.

ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS uid_validity VARCHAR;

ALTER TABLE emails ADD COLUMN IF NOT EXISTS uid_validity VARCHAR;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_email_org_uid'
    ) THEN
        ALTER TABLE emails DROP CONSTRAINT uq_email_org_uid;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_email_org_uidvalidity_uid'
    ) THEN
        ALTER TABLE emails ADD CONSTRAINT uq_email_org_uidvalidity_uid UNIQUE (org_id, uid_validity, uid);
    END IF;
END $$;
