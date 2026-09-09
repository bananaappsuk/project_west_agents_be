-- This project has no migration tool (no Alembic) — schema is normally created via
-- SQLAlchemy's create_all(), which only creates missing tables and never alters an
-- existing one. Safe to run multiple times (IF NOT EXISTS).
--
-- sync_epoch namespaces the dedup epoch (see pipeline.py's current_uid_validity)
-- per mailbox-configuration-generation, bumped whenever Settings points the
-- mailbox at a materially different connection (api.py's save_mailbox). Without
-- it, a provider reporting the same UIDVALIDITY across two different mailbox
-- generations (observed in practice — not all providers bump it on a full UID
-- renumbering) causes new mail to be wrongly treated as an already-synced
-- duplicate and silently dropped.

ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS sync_epoch INTEGER NOT NULL DEFAULT 0;
