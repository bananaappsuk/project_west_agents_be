-- Speaker-labelled transcript segments for the recording detail view.
--
-- This project has no migration tool: create_all() only creates missing tables and
-- never alters an existing one, so this column is applied by hand, like the others
-- here. Safe to run more than once (IF NOT EXISTS) and purely additive -- nullable,
-- no default, no rewrite of existing rows.
--
-- ORDER MATTERS. The voice agent deploys from `dev`, and the code that reads this
-- column selects it on every recording query. Apply this BEFORE that code is
-- deployed, or every recordings page fails with "column does not exist". Older code
-- ignores the extra column, so applying it early is harmless.

ALTER TABLE recordings ADD COLUMN IF NOT EXISTS transcript_segments JSONB;
