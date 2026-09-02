-- This project has no migration tool (no Alembic) — schema is normally created via
-- SQLAlchemy's create_all(), which only creates missing tables/indexes and never
-- alters an existing one in a way that would add this. Apply by hand. Safe to run
-- multiple times.
--
-- Why: this product moved from multi-tenant (self-serve org signup) to a single
-- organization, ever. /auth/register and /admin/provision — the two code paths
-- that used to create an Organization row — have both been removed; the org is
-- now created exactly once by scripts/seed_org.py. This index is the real
-- backstop: even if some future code path tried to insert a second
-- Organization row, Postgres itself refuses it, independent of what the
-- application code does or doesn't check.
--
-- How: a unique index on a constant expression. Every row evaluates the
-- expression `(true)` to the same value, so Postgres's uniqueness check across
-- that expression allows at most one row in the table, total.

CREATE UNIQUE INDEX IF NOT EXISTS ux_organizations_singleton ON organizations ((true));
