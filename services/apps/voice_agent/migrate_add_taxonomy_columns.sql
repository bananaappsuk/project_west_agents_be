-- This project has no migration tool (no Alembic) — schema is normally created via
-- SQLAlchemy's create_all(), which only creates missing tables and never alters an
-- existing one. Apply by hand. Safe to run multiple times.
--
-- Why: call recordings now classify into the same controlled Project West taxonomy as
-- email (Reference 1 of the Email Agent Capability Brief v1.1). One vocabulary across
-- both channels means a safeguarding disclosure reaches the restricted route whether it
-- arrived by phone or by email, and it is what makes the brief's section 8.3
-- cross-channel matching possible later.
--
-- See the mail_agent migration of the same name for the column rationale; this is the
-- voice-side equivalent.

ALTER TABLE recordings
    ADD COLUMN IF NOT EXISTS flags varchar[] NOT NULL DEFAULT '{}'::varchar[];

ALTER TABLE recordings
    ADD COLUMN IF NOT EXISTS route varchar NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS ix_recordings_route ON recordings (route);
CREATE INDEX IF NOT EXISTS ix_recordings_flags ON recordings USING gin (flags);

-- Recordings analysed under the old telecom-template taxonomy (Sales Enquiry, Booking,
-- Billing, ...) hold values with no PW meaning. Guessing a PW category from them would
-- be worse than admitting the gap, so they go to the exception queue for staff to
-- reclassify — the brief's own prescription for an item with no safe classification
-- (section 5.8). No recording, transcript or summary is touched.
UPDATE recordings
   SET category = 'unclassified',
       flags    = ARRAY['uncertain']::varchar[],
       route    = 'exception'
 WHERE category IN ('Sales Enquiry', 'Complaint', 'Support', 'Booking', 'Billing', 'General Enquiry')
    OR (category <> '' AND category <> lower(category));

UPDATE recordings
   SET route = 'exception'
 WHERE route = '';
