-- Project West mailbox + alias seed.
--
-- Runs AFTER migrate_pw_mailbox_model.sql. Safe to run more than once (idempotent on
-- address). Seeds system configuration only — no user-editable data, no credentials.
--
-- Every value below is a VERIFIED live-tenant fact (Exchange Online, 2026-10-06),
-- not an assumption from the capability brief. Where the brief and the tenant
-- disagreed, the tenant won:
--   * SIX physical shared mailboxes, not seven.
--   * admin@ is an ALIAS of management@, not a mailbox. Access Map v1 was wrong;
--     the September M365 report was right (MAILBOX_AUTHORIZATION_PLAN.md §16 d.10).
--
-- FAIL-CLOSED DEFAULTS where Project West has not yet decided:
--   * monitor_from IS NULL  -> the scheduler refuses to sync the mailbox at all.
--     Nothing is ingested until PW supplies a cutover/cutoff boundary (decision 4).
--     This is what stops a first sync walking 161,833 items in info@.
--   * admin@ reply_allowed = false, queue = '' (decision 11 outstanding).
--   * No mailbox_access_rule rows are seeded. With none, NOBODY has access — correct,
--     and the reason PW must supply Entra group object ids (decision 1) before the
--     feature can be switched on.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema='public' AND table_name='mailboxes'
                      AND column_name='graph_user_id') THEN
        RAISE EXCEPTION
            'PREREQUISITE MISSING: run migrate_pw_mailbox_model.sql first.';
    END IF;
END $$;

-- ── the six physical shared mailboxes ────────────────────────────────────────
-- graph_user_id values are the live ExternalDirectoryObjectId for each mailbox, so
-- no runtime Graph lookup is needed to start ingesting.
INSERT INTO mailboxes (
    id, kind, visible, enabled, provider,
    address, display_name, graph_user_id,
    monitor_from, first_sync_cap,
    imap_host, imap_port, smtp_host, smtp_port, username, password_enc
)
VALUES
    (gen_random_uuid()::varchar, 'pw', true, true, 'graph',
     'info@projectwest.co.uk',        'Info',        '9609fee8-549a-47b2-a2fc-e2e1c6d77e6d', NULL, 500, '', 993, '', 587, '', ''),
    (gen_random_uuid()::varchar, 'pw', true, true, 'graph',
     'referrals@projectwest.co.uk',   'Referrals',   'cbe6ae6b-9f76-4b02-84ea-0088f561e62d', NULL, 500, '', 993, '', 587, '', ''),
    (gen_random_uuid()::varchar, 'pw', true, true, 'graph',
     'coordinator@projectwest.co.uk', 'Coordinator', '29bad478-47bd-4c04-9143-c511a6b1dfcf', NULL, 500, '', 993, '', 587, '', ''),
    (gen_random_uuid()::varchar, 'pw', true, true, 'graph',
     'finance@projectwest.co.uk',     'Finance',     '9c4588ef-287d-4f6f-8d11-5f62d2ae29d1', NULL, 500, '', 993, '', 587, '', ''),
    (gen_random_uuid()::varchar, 'pw', true, true, 'graph',
     'management@projectwest.co.uk',  'Management',  '188f91f4-b916-4fed-8b88-5d30a81f5cde', NULL, 500, '', 993, '', 587, '', ''),
    (gen_random_uuid()::varchar, 'pw', true, true, 'graph',
     'reports@projectwest.co.uk',     'Reports',     '70e3685c-3a77-40d5-9d85-e9bb5e1ef4a3', NULL, 500, '', 993, '', 587, '', '')
ON CONFLICT (address) DO UPDATE
    SET graph_user_id = EXCLUDED.graph_user_id,
        display_name  = EXCLUDED.display_name,
        kind          = 'pw',
        provider      = 'graph';

-- ── the four business aliases ────────────────────────────────────────────────
-- safeguarding@ is reply_allowed=false and restricted=true: "no user and no agent
-- can send from it" until PW approves safeguarding leads (decision 3). That is data
-- configuration, not a code branch.
INSERT INTO mailbox_aliases (id, mailbox_id, address, queue, reply_allowed, restricted, business)
SELECT gen_random_uuid()::varchar, m.id, a.address, a.queue, a.reply_allowed, a.restricted, true
  FROM (VALUES
        ('payments@projectwest.co.uk',     'finance@projectwest.co.uk',     'payment_or_finance',      true,  false),
        ('complaints@projectwest.co.uk',   'coordinator@projectwest.co.uk', 'complaint',               true,  false),
        ('safeguarding@projectwest.co.uk', 'coordinator@projectwest.co.uk', 'safeguarding_restricted', false, true),
        -- decision 11 outstanding: queue and reply_allowed fail closed until PW decides.
        ('admin@projectwest.co.uk',        'management@projectwest.co.uk',  '',                        false, false)
       ) AS a(address, parent, queue, reply_allowed, restricted)
  JOIN mailboxes m ON m.address = a.parent
ON CONFLICT (address) DO UPDATE
    SET mailbox_id    = EXCLUDED.mailbox_id,
        queue         = EXCLUDED.queue,
        reply_allowed = EXCLUDED.reply_allowed,
        restricted    = EXCLUDED.restricted;

-- ── tenant routing addresses ─────────────────────────────────────────────────
-- Real delivery targets with no business meaning. Catalogued so mail arriving at one
-- RESOLVES rather than falling into the exception path as unresolved. business=false
-- keeps them out of anything that reasons about business entry points.
INSERT INTO mailbox_aliases (id, mailbox_id, address, queue, reply_allowed, restricted, business)
SELECT gen_random_uuid()::varchar, m.id, a.address, '', false, false, false
  FROM (VALUES
        ('referrals6151@ProjectWest27.onmicrosoft.com',  'referrals@projectwest.co.uk'),
        ('coordinator2744@ProjectWest27.onmicrosoft.com','coordinator@projectwest.co.uk'),
        ('finance@ProjectWest27.onmicrosoft.com',        'finance@projectwest.co.uk'),
        ('management@ProjectWest27.onmicrosoft.com',     'management@projectwest.co.uk')
       ) AS a(address, parent)
  JOIN mailboxes m ON m.address = a.parent
ON CONFLICT (address) DO NOTHING;

COMMIT;

-- ── verification (read-only; run after COMMIT) ───────────────────────────────
-- Expect: 6 pw mailboxes, 1 legacy, 8 aliases (4 business + 4 routing),
--         0 access rules, and every pw mailbox with monitor_from NULL (not yet synced).
--
-- SELECT kind, count(*) FROM mailboxes GROUP BY kind;
-- SELECT m.address AS mailbox, a.address AS alias, a.queue, a.reply_allowed,
--        a.restricted, a.business
--   FROM mailbox_aliases a JOIN mailboxes m ON m.id = a.mailbox_id
--  ORDER BY m.address, a.address;
-- SELECT count(*) AS access_rules FROM mailbox_access_rule;
