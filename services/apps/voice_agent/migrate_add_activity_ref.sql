-- Same manual-migration convention as migrate_add_content_uri.sql (no Alembic —
-- create_all() only creates missing tables, never alters an existing one).
-- Adds the CRM's POST /intake/activity reference, sent once per call regardless
-- of crm_status/crm_reference (the referral/case outcome, a separate thing).
-- Safe to run multiple times (IF NOT EXISTS).

ALTER TABLE recordings ADD COLUMN IF NOT EXISTS activity_ref TEXT;
