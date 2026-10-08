-- This project has no migration tool (no Alembic) — schema is normally created via
-- SQLAlchemy's create_all(), which only creates missing tables and never drops a
-- column. Apply by hand. Safe to run multiple times.
--
-- Why: see the mail_agent migration of the same name. This application serves Project
-- West only; the org_id columns and query predicates could only ever match the one
-- organization. The auth service's organizations/memberships/roles tables are
-- deliberately left alone, because Entra sign-in and RBAC depend on them.
--
-- ORDER MATTERS: replacement constraints go on before the columns they replace come
-- off, so there is no window in which a duplicate could be inserted.

-- ── recordings ────────────────────────────────────────────────────────────────
-- Dedup identity was (org_id, ext_id); with one tenant it is just (ext_id).
ALTER TABLE recordings DROP CONSTRAINT IF EXISTS uq_recording_org_ext;

-- If this fails, two rows share ext_id — genuine duplicates for a single tenant.
-- Inspect before forcing:
--   SELECT ext_id, count(*) FROM recordings GROUP BY 1 HAVING count(*) > 1;
ALTER TABLE recordings
    ADD CONSTRAINT uq_recording_ext UNIQUE (ext_id);

DROP INDEX IF EXISTS ix_recordings_org_id;
ALTER TABLE recordings DROP COLUMN IF EXISTS org_id;

-- ── voice_settings ────────────────────────────────────────────────────────────
-- org_id was UNIQUE here, so "exactly one settings row" came for free. The code still
-- relies on it (a bare scalar() select), so it is made explicit.
CREATE UNIQUE INDEX IF NOT EXISTS ux_voice_settings_singleton ON voice_settings ((true));
DROP INDEX IF EXISTS ix_voice_settings_org_id;
ALTER TABLE voice_settings DROP COLUMN IF EXISTS org_id;

-- ── voice_notifications / voice_agent_runs ────────────────────────────────────
DROP INDEX IF EXISTS ix_voice_notifications_org_id;
ALTER TABLE voice_notifications DROP COLUMN IF EXISTS org_id;

DROP INDEX IF EXISTS ix_voice_agent_runs_org_id;
ALTER TABLE voice_agent_runs DROP COLUMN IF EXISTS org_id;
