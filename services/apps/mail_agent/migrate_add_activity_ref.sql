-- Same manual-migration convention voice_agent uses (no Alembic — create_all()
-- only creates missing tables, never alters an existing one).
-- Adds the CRM's POST /intake/activity reference, sent once per email regardless
-- of crm_status/crm_reference (the referral/case outcome, a separate thing).
-- Safe to run multiple times (IF NOT EXISTS).

ALTER TABLE emails ADD COLUMN IF NOT EXISTS activity_ref TEXT;
