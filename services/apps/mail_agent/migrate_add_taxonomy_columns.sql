-- This project has no migration tool (no Alembic) — schema is normally created via
-- SQLAlchemy's create_all(), which only creates missing tables and never alters an
-- existing one. Apply by hand. Safe to run multiple times.
--
-- Why: the agent now classifies into the controlled Project West taxonomy defined by
-- Reference 1 of the Email Agent Capability Brief v1.1, which is two axes rather than
-- one -- a single primary category plus zero or more cross-cutting flags -- and records
-- the queue the item was routed to.
--
--   flags  -- urgent | sensitive | safeguarding_indicator | court_deadline | uncertain.
--            An array, not a second category column, because the brief's rule is that
--            a flag applies *in addition to* the primary category: a complaint that
--            discloses a safeguarding concern is still a complaint, and must carry
--            both.
--   route  -- the queue the item went to (referrals, finance, coordinator,
--            safeguarding_restricted, exception, ...). Stored rather than derived on
--            read, because the audit record in brief section 7.3 has to say where an
--            item actually went; re-tuning the routing table later must not silently
--            rewrite history.
--
-- `category` itself needs no DDL (it was already a free-text column) but its *values*
-- change meaning: the old template-era set (Billing, Support, Sales, Security,
-- General) is replaced by PW codes (new_referral, safeguarding_concern, ...). The
-- backfill at the bottom deals with rows classified under the old set.

ALTER TABLE emails
    ADD COLUMN IF NOT EXISTS flags varchar[] NOT NULL DEFAULT '{}'::varchar[];

ALTER TABLE emails
    ADD COLUMN IF NOT EXISTS route varchar NOT NULL DEFAULT '';

-- Finding every safeguarding item, and draining the exception queue, are both
-- routine screen-loads rather than rare reports, so they get indexes.
CREATE INDEX IF NOT EXISTS ix_emails_route ON emails (route);
CREATE INDEX IF NOT EXISTS ix_emails_flags ON emails USING gin (flags);

-- Rows analysed under the old taxonomy hold values that no longer mean anything.
-- Rather than guess a PW category from a template one -- "Support" could be a case
-- enquiry, a scheduling change or a complaint -- send them to the exception queue,
-- which is exactly what the brief prescribes for an item with no safe classification
-- (section 5.8). Staff reclassify them; nothing is deleted and no email is lost.
UPDATE emails
   SET category = 'unclassified',
       flags    = ARRAY['uncertain']::varchar[],
       route    = 'exception'
 WHERE category IN ('Billing', 'Support', 'Sales', 'Security', 'General')
    OR (category <> '' AND category <> lower(category));

-- Anything already classified but never routed (analysed before `route` existed).
UPDATE emails
   SET route = 'exception'
 WHERE route = '';
