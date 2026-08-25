-- This project has no migration tool (no Alembic) — schema is normally created via
-- SQLAlchemy's create_all(), which only creates missing tables and never alters an
-- existing one. The `emails` and `agent_config` tables already exist on Neon, so the
-- new columns added for AI auto-reply/HITL and HTML-body support must be applied by
-- hand with this script. Safe to run multiple times (IF NOT EXISTS).

ALTER TABLE emails ADD COLUMN IF NOT EXISTS content_type VARCHAR NOT NULL DEFAULT 'text';
ALTER TABLE emails ADD COLUMN IF NOT EXISTS confidence FLOAT NOT NULL DEFAULT 0.0;
ALTER TABLE emails ADD COLUMN IF NOT EXISTS needs_reply BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE emails ADD COLUMN IF NOT EXISTS auto_sent BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE agent_config ADD COLUMN IF NOT EXISTS auto_reply_enabled BOOLEAN NOT NULL DEFAULT false;
