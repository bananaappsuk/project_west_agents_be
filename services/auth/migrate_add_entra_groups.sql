-- This project has no migration tool (no Alembic) — schema is normally created via
-- SQLAlchemy's create_all(), which only creates missing tables and never alters an
-- existing one. Apply by hand BEFORE deploying the mailbox-access release.
-- Safe to run multiple times.
--
-- Why: the mail agent decides which shared mailboxes a person may read, send from
-- and approve for by matching their Entra security-group object ids against its own
-- mailbox_access_rule table (see apps/mail_agent/app/access_resolver.py).
--
-- Those ids arrive in the `groups` claim of the Entra access token at sign-in. But
-- POST /auth/refresh has no Entra token to read — it only has the refresh token —
-- so without somewhere to keep them, a user silently lost every mailbox the moment
-- their 15-minute access token rotated. Storing them on the user row is what makes
-- the groups survive a refresh.
--
-- Nullable on purpose, and distinct from an empty array: NULL means "we have no
-- group information for this user" (fail closed, investigate), whereas [] means
-- "this user is genuinely in no groups" (a real user with no access yet).

ALTER TABLE users ADD COLUMN IF NOT EXISTS entra_groups JSONB;
