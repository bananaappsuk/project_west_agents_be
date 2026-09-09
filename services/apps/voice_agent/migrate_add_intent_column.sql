-- Same manual-migration convention as the other migrate_*.sql files here (no
-- Alembic — schema is normally created via SQLAlchemy's create_all(), which
-- only creates missing tables and never alters an existing one). Safe to run
-- multiple times (IF NOT EXISTS).
--
-- `intent` was always computed in crm_sync.py to decide what to send the CRM,
-- but never persisted on the row (unlike mail_agent's Email.intent) — so the
-- UI had no way to show *why* a call's crm_status was what it was.

ALTER TABLE recordings ADD COLUMN IF NOT EXISTS intent VARCHAR NOT NULL DEFAULT 'NONE';
