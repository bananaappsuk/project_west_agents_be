-- This project has no migration tool (no Alembic) — schema is normally created via
-- SQLAlchemy's create_all(), which only creates missing tables and never alters an
-- existing one. The `emails` table already exists, so the new `intent` column (the
-- LLM's REFERRAL/CASE_COMMUNICATION/RESCHEDULE/CANCEL/NONE classification, now
-- persisted alongside crm_status/crm_reference so the frontend can show it) must be
-- applied by hand with this script. Safe to run multiple times (IF NOT EXISTS).

ALTER TABLE emails ADD COLUMN IF NOT EXISTS intent VARCHAR NOT NULL DEFAULT 'NONE';
