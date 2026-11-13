-- ============================================================================
-- Diagnostic: is the Entra `groups` claim arriving, and does it carry
-- PW-Voice-Users?   READ ONLY. PREPARED FOR REVIEW — NOT RUN.
-- ============================================================================
--
-- Every statement is a SELECT. There is no INSERT, UPDATE, DELETE or DDL in this
-- file, and nothing here is wrapped in a transaction because nothing needs undoing.
-- No token, secret or credential is read or printed: `users.entra_groups` holds
-- directory group object ids only.
--
-- Why this answers the question
-- ----------------------------
-- `_entra_groups` writes the claim's contents to `users.entra_groups` on every
-- sign-in (migrate_add_entra_groups.sql), because POST /auth/refresh has no Entra
-- token to re-read them from. That column is therefore a durable record of what
-- the `groups` claim actually contained the last time each person signed in — the
-- closest thing to a runtime log this system keeps, given that
-- `configure_logging` writes to stdout only and nothing captures it.
--
-- How to read the three possible states of the column
-- ---------------------------------------------------
--   NULL  -> this user has not signed in since the column was added, OR a sign-in
--            happened before `_entra_groups` existed. No evidence either way.
--   '[]'  -> a sign-in happened and the claim was ABSENT or in overage. This is the
--            "Access token box not ticked" symptom. Investigate.
--   [...] -> the claim arrived. Length tells you how many groups; D3 tells you
--            whether PW-Voice-Users is among them.
--
-- ⚠️ Timing. This reflects the LAST sign-in, not current Entra membership. The
-- Access-token groups claim was verified on PW Agents (Staging) on 2026-11-13 and
-- PW-Voice-Users is new, so anyone who last signed in before those changes will
-- show stale data. A populated row is proof the claim works; an empty or NULL row
-- is NOT proof it does not — it may simply pre-date the configuration.
--
-- Run (psql; DATABASE_URL is in backend/services/auth/.env):
--   psql "<auth database url>" -f diagnose_groups_claim_READONLY.sql
-- ============================================================================

\set voice_group_id '9245c941-a3f1-46d1-a225-1d20ba7dde8b'


-- ── D1. Is the claim arriving at all? One row per state. ────────────────────
-- The headline. If `claim_arrived` is 0 and `claim_absent_or_overage` is non-zero,
-- the Access-token groups claim is not reaching this service.
SELECT CASE
         WHEN u.entra_groups IS NULL                            THEN 'no_signin_recorded'
         WHEN jsonb_array_length(u.entra_groups) = 0             THEN 'claim_absent_or_overage'
         ELSE 'claim_arrived'
       END                                        AS state,
       count(*)                                   AS users,
       min(jsonb_array_length(u.entra_groups))    AS min_groups,
       max(jsonb_array_length(u.entra_groups))    AS max_groups
  FROM users u
 GROUP BY 1
 ORDER BY 1;


-- ── D2. Per user: did the claim arrive, and when did they last sign in? ─────
-- `last_token_issued_at` is the newest refresh-token row for that user, which is
-- created once per sign-in — a reliable proxy for "last signed in". Compare it
-- against the date the Entra token configuration was changed: a row older than
-- that change tells you nothing about the current configuration.
SELECT u.email,
       u.full_name,
       CASE
         WHEN u.entra_groups IS NULL                 THEN 'no_signin_recorded'
         WHEN jsonb_array_length(u.entra_groups) = 0 THEN 'claim_absent_or_overage'
         ELSE 'claim_arrived'
       END                                                       AS claim_state,
       COALESCE(jsonb_array_length(u.entra_groups), 0)            AS groups_in_claim,
       COALESCE(u.entra_groups @> to_jsonb(:'voice_group_id'::text), false) AS has_pw_voice_users,
       (SELECT max(rt.created_at) FROM refresh_tokens rt WHERE rt.user_id = u.id)
                                                                  AS last_token_issued_at
  FROM users u
 ORDER BY last_token_issued_at DESC NULLS LAST, u.email;


-- ── D3. The direct answer: who carries PW-Voice-Users today? ────────────────
-- These are the users who would be granted Voice at their next sign-in. Expect
-- this to be empty until an intended Voice member signs in AFTER being added to
-- the group — which is also the single cleanest runtime proof that the claim works.
SELECT u.email,
       u.full_name,
       jsonb_array_length(u.entra_groups) AS groups_in_claim,
       (SELECT max(rt.created_at) FROM refresh_tokens rt WHERE rt.user_id = u.id)
                                          AS last_token_issued_at
  FROM users u
 WHERE COALESCE(u.entra_groups @> to_jsonb(:'voice_group_id'::text), false)
 ORDER BY u.email;


-- ── D4. Cross-check against the mail agent's groups (different database) ────
-- The mail agent resolves mailbox access from the SAME claim, so if any mailbox
-- group is present here then the claim is demonstrably arriving and D1's
-- 'claim_absent_or_overage' rows are stale rather than broken.
--
-- Group ids are from apps/mail_agent/migrate_pw_seed_access_rules.sql. They live
-- in the mail agent's own database, hence the literals: this query runs against
-- the AUTH database and cannot join to that table.
SELECT u.email,
       COALESCE(u.entra_groups @> '["2c5a11c6-7f9c-4ba1-abf7-5ca143547b10"]'::jsonb, false) AS info,
       COALESCE(u.entra_groups @> '["d5347ccb-9793-4025-9988-e148e3af2a4c"]'::jsonb, false) AS referrals,
       COALESCE(u.entra_groups @> '["82c9db9a-d965-41ea-9e85-3c64152ed12b"]'::jsonb, false) AS coordinator,
       COALESCE(u.entra_groups @> '["766f94de-7c7e-4896-b821-eba597b788f7"]'::jsonb, false) AS finance,
       COALESCE(u.entra_groups @> '["9eb3e918-247d-4a9b-b9c8-f62fd6e62d70"]'::jsonb, false) AS management,
       COALESCE(u.entra_groups @> '["2429c8bb-c549-4adf-98c9-0110c22516f6"]'::jsonb, false) AS reports,
       COALESCE(u.entra_groups @> '["a5d90455-e675-458b-ae6d-3c915e22155f"]'::jsonb, false) AS safeguarding_lead
  FROM users u
 WHERE u.entra_groups IS NOT NULL AND jsonb_array_length(u.entra_groups) > 0
 ORDER BY u.email;


-- ── D5. Current voice-agent memberships, for the separate cleanup review ─────
-- Repeated from cleanup_voice_entitlement_REVIEW.sql section A1 so this file can
-- answer "has anything changed?" without opening that one. Read only.
SELECT count(*) AS voice_memberships_today
  FROM memberships m
  JOIN applications a ON a.id = m.app_id
 WHERE a.key = 'voice-agent';
