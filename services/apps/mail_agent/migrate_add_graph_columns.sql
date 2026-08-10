-- This project has no migration tool (no Alembic) — schema is normally created via
-- SQLAlchemy's create_all(), which only creates missing tables and never alters an
-- existing one. Since the `mailboxes` table already exists on Neon, the new Graph
-- columns added to the Mailbox model must be applied by hand with this script.
-- Safe to run multiple times (IF NOT EXISTS).

ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS provider VARCHAR NOT NULL DEFAULT 'imap';
ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS tenant_id VARCHAR;
ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS client_id VARCHAR;
ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS client_secret_enc TEXT;
