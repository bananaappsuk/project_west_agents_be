-- This project has no migration tool (no Alembic) — schema is normally created via
-- SQLAlchemy's create_all(), which only creates missing tables and never alters an
-- existing one. The `mailboxes` and `agent_runs` tables already exist on Neon, so
-- the new sync-watermark and archived-count columns must be applied by hand with
-- this script. Safe to run multiple times (IF NOT EXISTS).

ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS last_synced_uid VARCHAR;
ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ;

ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS archived INTEGER NOT NULL DEFAULT 0;
