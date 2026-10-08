-- Project West decision 7 ("who may approve?"), answered 2026-10-07.
--
-- PW's answer: whoever already has permission to reply from a shared mailbox is
-- also the person who approves replies from it. So approval follows send rights
-- rather than being a separate grant.
--
-- The capability brief treats approval as an axis distinct from sending, and the
-- schema keeps it that way -- can_approve remains its own column, and the scope
-- object still carries a separate `approvable` set. What changes is only its VALUE:
-- it is seeded equal to can_send. Keeping the axis means PW can later restrict
-- approval to a senior subset with a row update and no code change, which would be
-- impossible if the two had been collapsed into one flag.
--
-- The human gate is unaffected. The agent still never sends a substantive reply by
-- itself: a person must approve, and for restricted (safeguarding) items no
-- autonomous send is possible at all regardless of who holds approval.

BEGIN;

UPDATE mailbox_access_rule
   SET can_approve = can_send
 WHERE can_approve IS DISTINCT FROM can_send;

-- Existing resolved rows are rebuilt from the rules at next sign-in, but bring them
-- into line now so nobody is left with a stale grant in the meantime.
UPDATE user_mailbox_access
   SET can_approve = can_send
 WHERE can_approve IS DISTINCT FROM can_send;

COMMIT;
