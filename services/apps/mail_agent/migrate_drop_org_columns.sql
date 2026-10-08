-- This project has no migration tool (no Alembic) — schema is normally created via
-- SQLAlchemy's create_all(), which only creates missing tables and never drops a
-- column. Apply by hand. Safe to run multiple times.
--
-- Why: this application serves Project West and only Project West. Multi-tenancy was
-- already gone at the product level — self-serve signup and /admin/provision were
-- removed, the organization is created once by scripts/seed_org.py, and
-- ux_organizations_singleton makes Postgres itself refuse a second row. What was left
-- was the plumbing: an org_id column on every table and an org predicate on every
-- query, all of which could only ever match the one organization. This removes it.
--
-- Deliberately NOT removed: the `organizations`, `memberships` and `roles` tables in
-- the auth service. Entra ID sign-in and RBAC hang off those, that path works in
-- production, and rewriting it would buy nothing.
--
-- ORDER MATTERS: drop the old org-scoped constraints before the columns they cover,
-- and add the replacement singleton indexes before dropping the UNIQUE constraints
-- that currently provide that guarantee.

-- ── emails ────────────────────────────────────────────────────────────────────
-- The dedup identity was (org_id, uid_validity, uid); with one tenant it is just
-- (uid_validity, uid). Dropping the column would take the constraint with it, so the
-- replacement goes on first — there must be no window in which a duplicate import
-- could slip through.
ALTER TABLE emails DROP CONSTRAINT IF EXISTS uq_email_org_uidvalidity_uid;

-- If this fails, two rows share (uid_validity, uid) — which with a single tenant means
-- genuine duplicates. Inspect before forcing:
--   SELECT uid_validity, uid, count(*) FROM emails
--    GROUP BY 1,2 HAVING count(*) > 1;
ALTER TABLE emails
    ADD CONSTRAINT uq_email_uidvalidity_uid UNIQUE (uid_validity, uid);

DROP INDEX IF EXISTS ix_emails_org_id;
ALTER TABLE emails DROP COLUMN IF EXISTS org_id;

-- ── mailboxes ─────────────────────────────────────────────────────────────────
-- org_id was UNIQUE here, so "exactly one mailbox" was a side effect of "exactly one
-- org". That property is still relied on by the code (pipeline.py and api.py both
-- select the mailbox with a bare scalar()), so it is made explicit rather than lost
-- silently along with the column.
--
-- This index is what gets dropped when multi-mailbox support lands: the capability
-- brief needs six monitored shared mailboxes plus alias routing onto them.
CREATE UNIQUE INDEX IF NOT EXISTS ux_mailboxes_singleton ON mailboxes ((true));
DROP INDEX IF EXISTS ix_mailboxes_org_id;
ALTER TABLE mailboxes DROP COLUMN IF EXISTS org_id;

-- ── agent_config ──────────────────────────────────────────────────────────────
CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_config_singleton ON agent_config ((true));
DROP INDEX IF EXISTS ix_agent_config_org_id;
ALTER TABLE agent_config DROP COLUMN IF EXISTS org_id;

-- ── folders / agent_runs ──────────────────────────────────────────────────────
-- Plain per-tenant partitioning with nothing else hanging off it.
DROP INDEX IF EXISTS ix_folders_org_id;
ALTER TABLE folders DROP COLUMN IF EXISTS org_id;

DROP INDEX IF EXISTS ix_agent_runs_org_id;
ALTER TABLE agent_runs DROP COLUMN IF EXISTS org_id;
