-- Project West mailbox access rules (decision 1), applied 2026-10-07.
--
-- Rules bind to Entra GROUPS, never to individual users. PW IT adds someone to a
-- group in Entra and their access changes at next sign-in -- no deployment, no
-- application change, and joiner/leaver handling stays where it belongs. This table
-- stores which GROUP grants which mailbox (a handful of stable rows) and never stores
-- who is in it.
--
-- Group object ids are live Entra values, created 2026-10-07 as SECURITY groups
-- (MailEnabled=false). Deliberately not the pre-existing Microsoft 365 groups in the
-- tenant: those are mail-enabled Teams/SharePoint groups whose membership reflects
-- site history rather than mailbox entitlement, and using one for authorization would
-- mean adding someone to a Teams site silently granted them mailbox access.
--
-- can_read / can_send are granted TOGETHER, because the live Exchange permission
-- matrix grants Full Access and Send As together in every single cell -- so decision 6
-- ("one grant or two?") is answered by evidence rather than preference. One group per
-- mailbox, not three.
--
-- can_approve is FALSE everywhere, deliberately. The SSOT treats approval as an axis
-- distinct from sending but does not say who holds it (decision 7, still open). False
-- is the fail-closed default; it does mean the approval workflow cannot be exercised
-- until PW answers.
--
-- PW-Safeguarding-Lead (a5d90455-e675-458b-ae6d-3c915e22155f) gets NO row here: it is
-- a clearance axis, not a mailbox grant, and it is resolved separately. It is also
-- intentionally EMPTY until PW names safeguarding leads (decision 3), which means
-- nobody can see restricted items. Correct failure direction, deliberately chosen.

BEGIN;

INSERT INTO mailbox_access_rule
    (id, entra_group_id, entra_group_name, mailbox_id, can_read, can_send, can_approve)
SELECT gen_random_uuid()::varchar, g.group_id, g.group_name, m.id, true, true, false
  FROM (VALUES
        ('2c5a11c6-7f9c-4ba1-abf7-5ca143547b10', 'PW-Mailbox-Info',        'info@projectwest.co.uk'),
        ('d5347ccb-9793-4025-9988-e148e3af2a4c', 'PW-Mailbox-Referrals',   'referrals@projectwest.co.uk'),
        ('82c9db9a-d965-41ea-9e85-3c64152ed12b', 'PW-Mailbox-Coordinator', 'coordinator@projectwest.co.uk'),
        ('766f94de-7c7e-4896-b821-eba597b788f7', 'PW-Mailbox-Finance',     'finance@projectwest.co.uk'),
        ('9eb3e918-247d-4a9b-b9c8-f62fd6e62d70', 'PW-Mailbox-Management',  'management@projectwest.co.uk'),
        ('2429c8bb-c549-4adf-98c9-0110c22516f6', 'PW-Mailbox-Reports',     'reports@projectwest.co.uk')
       ) AS g(group_id, group_name, mailbox_address)
  JOIN mailboxes m ON m.address = g.mailbox_address AND m.kind = 'pw'
ON CONFLICT (entra_group_id, mailbox_id) DO UPDATE
    SET entra_group_name = EXCLUDED.entra_group_name,
        can_read         = EXCLUDED.can_read,
        can_send         = EXCLUDED.can_send,
        can_approve      = EXCLUDED.can_approve;

COMMIT;
