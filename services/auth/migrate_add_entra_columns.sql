-- This project has no migration tool (no Alembic) — schema is normally created via
-- SQLAlchemy's create_all(), which only creates missing tables and never alters an
-- existing one. Apply by hand BEFORE deploying the Entra sign-in release. Safe to
-- run multiple times.
--
-- Why: sign-in moved from email/password to Microsoft Entra ID (POST /auth/entra).
-- Users are now looked up by their immutable Entra object id (`oid`). An existing row is
-- bound to its Entra account once, on first sign-in (UPN = email; see routers/entra.py).

-- 1. Link columns. entra_oid is unique: one local user per Entra user.
ALTER TABLE users ADD COLUMN IF NOT EXISTS entra_oid VARCHAR;
ALTER TABLE users ADD COLUMN IF NOT EXISTS entra_upn VARCHAR;
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_entra_oid ON users (entra_oid);

-- 2. Users provisioned by Entra sign-in have no password.
ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;

-- 3. End every session issued by password login. Those refresh tokens were minted
--    with a 30-day lifetime; the new policy caps a session at 8 hours from sign-in,
--    and everyone should come back through Microsoft sign-in.
UPDATE refresh_tokens SET revoked = true WHERE revoked = false;

-- The invites and password_resets tables are no longer used by the code. They are
-- left in place (dropping is irreversible); drop them manually once you're sure:
--   DROP TABLE IF EXISTS invites;
--   DROP TABLE IF EXISTS password_resets;
