-- This project has no migration tool (no Alembic) — schema is normally created via
-- SQLAlchemy's create_all(), which only creates missing tables and never alters an
-- existing one. The `agent_runs` table already exists on Neon, so the new column
-- added to surface why a background sync run failed must be applied by hand with
-- this script. Safe to run multiple times (IF NOT EXISTS).
--
-- No schema change is needed for AgentRun.status gaining a "running" value — it
-- was always a free-text VARCHAR column.

ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS error_message TEXT;
