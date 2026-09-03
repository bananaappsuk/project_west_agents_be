-- This project has no migration tool (no Alembic) — schema is normally created via
-- SQLAlchemy's create_all(), which only creates missing tables and never alters an
-- existing one. The `recordings` table already exists on Neon, so the new column
-- that lets BT Cloud recordings be played back on demand (see bt_client.fetch_audio /
-- api.py's GET /recordings/{id}/audio) must be applied by hand with this script.
-- Safe to run multiple times (IF NOT EXISTS).
--
-- Recordings synced before this migration runs will have content_uri = NULL and stay
-- non-playable (audioAvailable=false) until they're synced again — there's no cheap
-- way to retroactively recover a specific call's contentUri without re-listing it.

ALTER TABLE recordings ADD COLUMN IF NOT EXISTS content_uri TEXT;
