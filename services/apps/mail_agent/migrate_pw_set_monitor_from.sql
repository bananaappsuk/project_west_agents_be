-- Project West ingestion boundaries (decision 4), approved 2026-10-07.
--
-- Until this runs every PW mailbox has monitor_from = NULL and the scheduler refuses
-- to sync it, which is the fail-closed default. This supplies the boundary.
--
-- Sized from the LIVE INBOX counts obtained via Graph, not from
-- Get-EXOMailboxStatistics. That distinction matters: the statistics cmdlet counts
-- the whole mailbox including Sent Items, Drafts and Deleted, and the agent polls the
-- Inbox. info@ is 85,120 inbound, not the 161,833 total the earlier figure implied --
-- roughly 54,000 of that mailbox is Sent Items the agent will never read.
--
--   full history : the three mailboxes that carry the business workflow, and whose
--                  inbound volume is trivial (63 + 35 + 1,843 = 1,941 items).
--   6 months     : finance@ -- payments@ routing matters, but 8,878 items of
--                  historical finance mail does not need an agent.
--   cutover      : reports@ (largely automated output) and info@ (85,120 inbound).
--                  Historical ingestion of these is not a cost decision.
--
-- Safe to run more than once: each statement is keyed on address and sets an
-- absolute value rather than an offset.

BEGIN;

-- Full history. 2000-01-01 is safely before anything in the tenant (the oldest
-- filed mail observed is 2007), so it means "everything in the Inbox".
UPDATE mailboxes SET monitor_from = TIMESTAMPTZ '2000-01-01 00:00:00+00'
 WHERE kind = 'pw'
   AND address IN ('referrals@projectwest.co.uk',
                   'coordinator@projectwest.co.uk',
                   'management@projectwest.co.uk');

-- Last 6 months.
UPDATE mailboxes SET monitor_from = (now() - INTERVAL '6 months')
 WHERE kind = 'pw' AND address = 'finance@projectwest.co.uk';

-- Cutover: from the moment the boundary is set. Nothing historical is read.
UPDATE mailboxes SET monitor_from = now()
 WHERE kind = 'pw'
   AND address IN ('reports@projectwest.co.uk', 'info@projectwest.co.uk');

COMMIT;
