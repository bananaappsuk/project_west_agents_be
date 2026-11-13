-- ============================================================================
-- Voice Agent entitlement cleanup — PREPARED FOR REVIEW. NOT RUN.
-- ============================================================================
--
-- Why this exists
-- ---------------
-- Before the entitlement change in app/routers/entra.py, POST /auth/entra looped
-- over every agent app and created a Membership for each one if it was absent. So
-- every user who has ever signed in holds a `voice-agent` membership, whether or
-- not anyone decided they should.
--
-- Gating token issuance (the code change) fixes new sign-ins. It does NOT clear
-- what is already there, and a leftover membership is not inert: POST /auth/refresh
-- re-reads the membership row and mints a fresh access token whenever one exists
-- (routers/auth.py), so a stale membership plus a refresh token still inside its
-- 8-hour session keeps renewing Voice access. This script is what makes the gate
-- retrospective.
--
-- Run ORDER matters
-- -----------------
--   1. Deploy the auth change FIRST. Running this against the old code achieves
--      nothing: the next sign-in re-creates every row it deletes.
--   2. Then run section A (read-only) and send the output for approval.
--   3. Only then run section C.
--
-- What it does NOT do
-- -------------------
-- It does not touch `mail-agent` memberships, any `users` row, any mailbox access
-- rule, or any safeguarding clearance. Email is untouched by design.
--
-- Re-grant is automatic: a user added to PW-Voice-Users gets a membership and a
-- token back at their next sign-in, with no database work.
--
-- ⚠️ Note on timing — read this before interpreting section A.
--
-- `users.entra_groups` holds the group ids seen at that user's LAST sign-in, not
-- their current Entra membership. PW-Voice-Users now exists
-- (9245c941-a3f1-46d1-a225-1d20ba7dde8b), but a member who has not signed in SINCE
-- being added to it still has a stored group list without it — and A2/A3 would
-- therefore list them for removal even though they are entitled.
--
-- So A3 is "who the database currently believes is unentitled", which is a superset
-- of who actually is. Two ways to handle it, both fine:
--
--   * have the intended Voice members sign in once (after the auth change is
--     deployed), then re-run section A — their rows drop out of A3 by themselves; or
--   * accept that C removes them too, and let their next sign-in re-grant it. No
--     database work is needed for the re-grant.
--
-- Either way the end state is the same. The difference is only whether a member
-- briefly loses access between the cleanup and their next sign-in.
-- ============================================================================


-- ============================================================================
-- SECTION A — READ ONLY. Run this first. Nothing here changes any data.
-- ============================================================================

-- A0. PW-Voice-Users, as confirmed by Project West on 2026-11-12. Must match the
--     ENTRA_VOICE_GROUP_ID the auth service is deployed with, or these queries and
--     the running service will disagree about who is entitled.
--     (psql syntax. Running this through another client? Substitute the literal.)
\set voice_group_id '9245c941-a3f1-46d1-a225-1d20ba7dde8b'

-- A1. Affected-user COUNT: how many users hold a voice-agent membership today.
SELECT count(*) AS voice_memberships_total
  FROM memberships m
  JOIN applications a ON a.id = m.app_id
 WHERE a.key = 'voice-agent';

-- A2. The COUNT that actually matters: voice memberships held by users whose last
--     sign-in did not show them in PW-Voice-Users.
SELECT count(*) AS voice_memberships_to_remove
  FROM memberships m
  JOIN applications a ON a.id = m.app_id
  JOIN users u        ON u.id = m.user_id
 WHERE a.key = 'voice-agent'
   AND NOT COALESCE(u.entra_groups @> to_jsonb(:'voice_group_id'::text), false);

-- A3. The PROPOSED CLEANUP LIST — the rows section C would delete, named.
--     This is the list to review and approve. `live_refresh_tokens` is how many
--     sessions would stop working at their next rotation.
SELECT u.email,
       u.full_name,
       u.entra_oid,
       m.id                               AS membership_id,
       jsonb_array_length(COALESCE(u.entra_groups, '[]'::jsonb)) AS groups_at_last_signin,
       (SELECT count(*) FROM refresh_tokens rt
         WHERE rt.user_id = u.id AND rt.app_id = m.app_id AND rt.revoked = false
           AND rt.expires_at > now())      AS live_refresh_tokens
  FROM memberships m
  JOIN applications a ON a.id = m.app_id
  JOIN users u        ON u.id = m.user_id
 WHERE a.key = 'voice-agent'
   AND NOT COALESCE(u.entra_groups @> to_jsonb(:'voice_group_id'::text), false)
 ORDER BY u.email;

-- A4. The users who WOULD keep Voice (the group was visible at last sign-in).
--     Expected to be empty until PW-Voice-Users exists and its members sign in.
SELECT u.email, u.full_name
  FROM memberships m
  JOIN applications a ON a.id = m.app_id
  JOIN users u        ON u.id = m.user_id
 WHERE a.key = 'voice-agent'
   AND COALESCE(u.entra_groups @> to_jsonb(:'voice_group_id'::text), false)
 ORDER BY u.email;

-- A5. Control check — Email must be untouched. Record this before and after;
--     the two numbers must be identical.
SELECT count(*) AS mail_memberships_unchanged_control
  FROM memberships m
  JOIN applications a ON a.id = m.app_id
 WHERE a.key = 'mail-agent';


-- ============================================================================
-- SECTION B — what a Voice membership currently grants, for the record.
-- Read only. Useful evidence for the change record.
-- ============================================================================

SELECT a.key AS app, r.name AS role, p.key AS permission, count(DISTINCT m.user_id) AS users
  FROM memberships m
  JOIN applications a      ON a.id = m.app_id
  JOIN membership_roles mr ON mr.membership_id = m.id
  JOIN roles r             ON r.id = mr.role_id
  JOIN role_permissions rp ON rp.role_id = r.id
  JOIN permissions p       ON p.id = rp.permission_id
 WHERE a.key = 'voice-agent'
 GROUP BY a.key, r.name, p.key
 ORDER BY r.name, p.key;


-- ============================================================================
-- SECTION C — THE CLEANUP. DO NOT RUN WITHOUT EXPLICIT APPROVAL OF SECTION A3.
-- ============================================================================
-- Wrapped in an explicit transaction and ending in ROLLBACK, so that pasting the
-- whole block by accident changes nothing. Swap ROLLBACK for COMMIT deliberately,
-- as the last edit you make, after reading the row counts it prints.
--
-- C1 before C2: revoking the tokens first means there is no window in which a
-- membership is gone but a refresh is still in flight against it.

BEGIN;

-- C1. End the Voice sessions. Not strictly required for correctness — /auth/refresh
--     answers 403 "membership revoked" once C2 has run — but it closes the gap
--     immediately instead of at the next rotation (<= 15 minutes).
UPDATE refresh_tokens rt
   SET revoked = true
 WHERE rt.revoked = false
   AND rt.app_id IN (SELECT id FROM applications WHERE key = 'voice-agent')
   AND rt.user_id IN (
         SELECT u.id FROM users u
          WHERE NOT COALESCE(u.entra_groups @> to_jsonb(:'voice_group_id'::text), false)
       );

-- C2. Remove the memberships. `membership_roles` rows go with them via
--     ON DELETE CASCADE (see models.py's membership_roles table definition), so
--     there is no orphan association row to clean up separately.
DELETE FROM memberships m
 USING applications a, users u
 WHERE a.id = m.app_id
   AND u.id = m.user_id
   AND a.key = 'voice-agent'
   AND NOT COALESCE(u.entra_groups @> to_jsonb(:'voice_group_id'::text), false);

-- C3. Verify inside the transaction, before deciding to commit.
--     Expected: voice_remaining = the A4 count; mail_control = the A5 count.
SELECT
  (SELECT count(*) FROM memberships m JOIN applications a ON a.id = m.app_id
    WHERE a.key = 'voice-agent') AS voice_remaining,
  (SELECT count(*) FROM memberships m JOIN applications a ON a.id = m.app_id
    WHERE a.key = 'mail-agent')  AS mail_control;

ROLLBACK;   -- <-- change to COMMIT only when the counts above are what you expect
