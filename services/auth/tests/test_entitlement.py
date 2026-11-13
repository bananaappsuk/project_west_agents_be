"""Per-application entitlement at POST /auth/entra (app/routers/entra.py).

Run from backend/services/auth with the venv active:
    python -m unittest discover -s tests

Two layers, because "the rule is right" and "the endpoint applies the rule" are
different claims and only the second is what the ticket asks for:

  * `EntitlementRuleTests` exercise `_entitled_apps` directly. It is pure -- no
    database, no session, no Entra -- so every branch is reachable cheaply.
  * `SignInEntitlementTests` drive the real `entra_sign_in` handler with the
    database and token minting stubbed, and assert which apps actually got a
    token, which memberships were created, and which were revoked. A rule that is
    correct but wired up to the wrong loop passes the first class and fails this
    one.

No aiosqlite in this service's venv, so the session is a stub rather than a real
in-memory database. The stub records what the handler asked it to do, which is
what these assertions are about.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from app.config import settings
from app.routers import entra
from app.schemas import TokenOut

MAIL = "mail-agent"
VOICE = "voice-agent"

OID = "33333333-3333-3333-3333-333333333333"
VOICE_GROUP = "aaaaaaaa-1111-2222-3333-444444444444"
OTHER_GROUP = "bbbbbbbb-5555-6666-7777-888888888888"


# ─────────────────────────────────────────────────────────────────────────────
# The rule
# ─────────────────────────────────────────────────────────────────────────────

class EntitlementRuleTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(settings, "entra_voice_group_id", VOICE_GROUP)
        patcher.start()
        self.addCleanup(patcher.stop)

    def entitled(self, *, role="User", groups=()):
        return entra._entitled_apps(role=role, groups=list(groups), oid=OID)

    # --- Email: unchanged by this work ---------------------------------------

    def test_app_role_alone_entitles_email(self):
        self.assertEqual(self.entitled(groups=[]), (MAIL,))

    def test_email_entitlement_does_not_depend_on_any_group(self):
        self.assertEqual(self.entitled(groups=[OTHER_GROUP]), (MAIL,))

    def test_every_role_entitles_email(self):
        for role in ("User", "Admin", "Superadmin"):
            with self.subTest(role=role):
                self.assertIn(MAIL, self.entitled(role=role, groups=[]))

    def test_email_entitlement_survives_voice_being_unconfigured(self):
        with mock.patch.object(settings, "entra_voice_group_id", None):
            self.assertEqual(self.entitled(groups=[VOICE_GROUP]), (MAIL,))

    # --- Voice: group membership required ------------------------------------

    def test_voice_group_member_is_entitled_to_voice(self):
        self.assertEqual(self.entitled(groups=[VOICE_GROUP]), (MAIL, VOICE))

    def test_voice_group_among_others_still_entitles(self):
        self.assertEqual(self.entitled(groups=[OTHER_GROUP, VOICE_GROUP]), (MAIL, VOICE))

    def test_non_member_is_not_entitled_to_voice(self):
        self.assertNotIn(VOICE, self.entitled(groups=[OTHER_GROUP]))

    def test_group_id_match_ignores_case_and_surrounding_space(self):
        # Entra and PW IT hand these over as text; a pasted id with different
        # casing must not silently revoke a named lead's access.
        self.assertIn(VOICE, self.entitled(groups=[f"  {VOICE_GROUP.upper()}  "]))

    def test_configured_group_id_is_compared_after_trimming(self):
        with mock.patch.object(settings, "entra_voice_group_id", f" {VOICE_GROUP} "):
            self.assertIn(VOICE, self.entitled(groups=[VOICE_GROUP]))

    # --- Fail closed ---------------------------------------------------------

    def test_no_configured_group_denies_voice_to_everyone(self):
        # The pre-change default was the opposite: everyone got Voice. An unset
        # group id must not reproduce that.
        for value in (None, "", "   "):
            with self.subTest(value=repr(value)):
                with mock.patch.object(settings, "entra_voice_group_id", value):
                    self.assertEqual(self.entitled(groups=[VOICE_GROUP]), (MAIL,))

    def test_empty_groups_claim_denies_voice(self):
        # `_entra_groups` returns [] for a group overage or a registration that is
        # not emitting the claim. Denying Voice there is the safe answer.
        self.assertEqual(self.entitled(groups=[]), (MAIL,))

    def test_blank_group_entries_are_ignored(self):
        self.assertNotIn(VOICE, self.entitled(groups=["", None, "   "]))

    def test_no_role_entitles_nothing(self):
        self.assertEqual(self.entitled(role=None, groups=[VOICE_GROUP]), ())

    def test_group_membership_alone_is_not_a_grant(self):
        # Assignment to the enterprise app is still required; the group widens
        # what an assigned user may reach, it does not replace assignment.
        self.assertEqual(entra._entitled_apps(role=None, groups=[VOICE_GROUP]), ())

    def test_result_is_a_stable_ordered_tuple(self):
        self.assertEqual(self.entitled(groups=[VOICE_GROUP]), (MAIL, VOICE))


# ─────────────────────────────────────────────────────────────────────────────
# The endpoint
# ─────────────────────────────────────────────────────────────────────────────

class _Result:
    def __init__(self, rowcount=0):
        self.rowcount = rowcount


class FakeSession:
    """Just enough AsyncSession for `entra_sign_in`, and a record of what it did.

    `scalar`/`scalars` are driven by `memberships`, which the test seeds to stand
    for rows already in the database -- which is how the "a stale membership is
    removed" case is set up without a database.
    """

    def __init__(self, *, memberships=()):
        # app_key -> a stand-in membership object
        self.memberships = dict(memberships)
        self.deleted: list[str] = []
        self.added: list[object] = []
        self.refresh_revoked: list[str] = []
        self.committed = 0
        self._org = SimpleNamespace(id="org-1")

    async def scalar(self, stmt):
        text = str(stmt)
        if "FROM organizations" in text:
            return self._org
        if "FROM memberships" in text:
            return self.memberships.get(self._app_key_in(text))
        if "FROM applications" in text:
            return None  # `_app_for` then creates one
        return None

    async def scalars(self, stmt):
        text = str(stmt)
        if "FROM memberships" in text:
            key = self._pending_revoke_app
            found = self.memberships.get(key)
            return iter([found] if found else [])
        return iter([])

    async def execute(self, stmt):
        text = str(stmt)
        if "UPDATE refresh_tokens" in text:
            self.refresh_revoked.append(self._pending_revoke_app)
            return _Result(rowcount=1)
        return _Result()

    async def delete(self, obj):
        self.deleted.append(getattr(obj, "app_key", "?"))
        self.memberships.pop(getattr(obj, "app_key", None), None)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        self.committed += 1

    # The handler calls `_revoke_app_access(session, user=..., app=...)`, and the
    # statements it builds do not carry the app key as text, so the test tracks
    # which app the handler is currently working on via the patched `_app_for`.
    _pending_revoke_app = None

    def _app_key_in(self, text):
        return self._pending_revoke_app


def _run(coro):
    return asyncio.run(coro)


class SignInEntitlementTests(unittest.TestCase):
    """Drives the real handler. Only the edges are stubbed."""

    def setUp(self):
        self.session = FakeSession()
        self.user = SimpleNamespace(id="user-1", email="a@b.c", entra_groups=None)

        def app_for(session, key):
            session._pending_revoke_app = key
            return SimpleNamespace(id=f"app-{key}", key=key)

        async def _app_for(session, key):
            return app_for(session, key)

        async def _find_or_provision_user(session, claims):
            return self.user

        async def _ensure_role(session, app, org_id, display_role):
            return SimpleNamespace(name="user", permissions=[])

        self.issued: list[str] = []

        async def _issue_tokens(session, *, user, org_id, app, roles, scope, **kw):
            self.issued.append(app.key)
            # A real TokenOut, not a stand-in: EntraTokensOut validates its values,
            # so a stub of the wrong type would fail the response rather than the
            # behaviour being tested.
            return TokenOut(
                access_token=f"access-{app.key}",
                expires_in=900,
                refresh_token=f"refresh-{app.key}",
            )

        def _token_fields(app_key, membership):
            return ([f"{app_key}:user"], f"{app_key}:emails.read")

        for patcher in (
            mock.patch.object(entra, "_app_for", _app_for),
            mock.patch.object(entra, "_find_or_provision_user", _find_or_provision_user),
            mock.patch.object(entra, "_ensure_role", _ensure_role),
            mock.patch.object(entra, "_issue_tokens", _issue_tokens),
            mock.patch.object(entra, "_token_fields", _token_fields),
            mock.patch.object(settings, "entra_voice_group_id", VOICE_GROUP),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def sign_in(self, *, groups, roles=("Agents.User",)):
        claims = {"oid": OID, "tid": "t", "groups": list(groups), "roles": list(roles)}

        async def verify(_token):
            return claims

        with mock.patch.object(entra, "verify_access_token", verify):
            return _run(entra.entra_sign_in(authorization="Bearer x", session=self.session))

    # --- A1-A4: who gets which token -----------------------------------------

    def test_email_only_user_gets_no_voice_token(self):
        out = self.sign_in(groups=[OTHER_GROUP])
        self.assertEqual(set(out.tokens), {MAIL})
        self.assertNotIn(VOICE, out.tokens)
        self.assertEqual(self.issued, [MAIL])

    def test_email_only_user_still_gets_a_working_email_token(self):
        out = self.sign_in(groups=[OTHER_GROUP])
        self.assertEqual(out.tokens[MAIL].access_token, f"access-{MAIL}")
        self.assertEqual(out.tokens[MAIL].refresh_token, f"refresh-{MAIL}")

    def test_voice_group_member_gets_both_tokens(self):
        out = self.sign_in(groups=[VOICE_GROUP])
        self.assertEqual(set(out.tokens), {MAIL, VOICE})
        self.assertEqual(self.issued, [MAIL, VOICE])

    def test_user_with_no_role_gets_the_existing_no_access_response(self):
        with self.assertRaises(HTTPException) as ctx:
            self.sign_in(groups=[VOICE_GROUP], roles=())
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, entra.NO_ACCESS)
        self.assertEqual(self.issued, [])

    # --- A5: no automatic provisioning ---------------------------------------

    def test_no_membership_is_created_for_an_unentitled_app(self):
        self.sign_in(groups=[OTHER_GROUP])
        created = [getattr(m, "app_id", None) for m in self.session.added]
        self.assertIn(f"app-{MAIL}", created)
        self.assertNotIn(f"app-{VOICE}", created)

    def test_membership_is_created_for_an_entitled_app(self):
        self.sign_in(groups=[VOICE_GROUP])
        created = [getattr(m, "app_id", None) for m in self.session.added]
        self.assertIn(f"app-{VOICE}", created)

    # --- withdrawal: a stale membership is removed ---------------------------

    def test_existing_voice_membership_is_revoked_when_not_entitled(self):
        self.session.memberships[VOICE] = SimpleNamespace(
            app_key=VOICE, app_id=f"app-{VOICE}", roles=[]
        )
        self.sign_in(groups=[OTHER_GROUP])
        self.assertIn(VOICE, self.session.deleted)
        self.assertIn(VOICE, self.session.refresh_revoked)

    def test_email_membership_is_never_revoked_by_a_voice_denial(self):
        self.session.memberships[MAIL] = SimpleNamespace(
            app_key=MAIL, app_id=f"app-{MAIL}", roles=[]
        )
        self.sign_in(groups=[OTHER_GROUP])
        self.assertNotIn(MAIL, self.session.deleted)
        self.assertNotIn(MAIL, self.session.refresh_revoked)

    # --- fail closed, end to end ---------------------------------------------

    def test_unconfigured_voice_group_issues_no_voice_token(self):
        with mock.patch.object(settings, "entra_voice_group_id", None):
            out = self.sign_in(groups=[VOICE_GROUP])
        self.assertEqual(set(out.tokens), {MAIL})

    def test_missing_groups_claim_issues_no_voice_token(self):
        out = self.sign_in(groups=[])
        self.assertEqual(set(out.tokens), {MAIL})

    def test_groups_are_persisted_on_the_user_row(self):
        self.sign_in(groups=[VOICE_GROUP])
        self.assertEqual(self.user.entra_groups, [VOICE_GROUP])

    def test_sign_in_commits_once(self):
        self.sign_in(groups=[VOICE_GROUP])
        self.assertEqual(self.session.committed, 1)


# ─────────────────────────────────────────────────────────────────────────────
# The guard that keeps being true
# ─────────────────────────────────────────────────────────────────────────────

class NoUnconditionalProvisioningTests(unittest.TestCase):
    """Static check over the handler's source.

    Every behavioural test above passes for a handler that loops over
    `AGENT_APPS` and then filters afterwards -- and also for one that stops
    filtering at all, if a future edit reorders it. This asserts the structural
    property instead: the entitlement decision is consulted in the loop, and
    `_issue_tokens` is not reachable without it.
    """

    def setUp(self):
        import inspect
        self.src = inspect.getsource(entra.entra_sign_in)

    def test_handler_consults_the_entitlement_rule(self):
        self.assertIn("_entitled_apps(", self.src)

    def test_handler_skips_unentitled_apps_before_issuing_tokens(self):
        self.assertIn("not in entitled", self.src)
        skip_at = self.src.index("not in entitled")
        issue_at = self.src.index("_issue_tokens(")
        self.assertLess(skip_at, issue_at, "entitlement must be checked before a token is minted")

    def test_handler_revokes_rather_than_ignoring_a_stale_membership(self):
        self.assertIn("_revoke_app_access(", self.src)


# ─────────────────────────────────────────────────────────────────────────────
# The backfill script must not hand Voice back
# ─────────────────────────────────────────────────────────────────────────────

class BackfillGuardTests(unittest.TestCase):
    """`scripts/backfill_companion_access.py` predates per-app entitlement.

    Its whole purpose was to grant whichever agent app a user did not have, so one
    `--apply` run would re-grant Voice to every Email user and silently override the
    sign-in decision -- `POST /auth/refresh` mints a token from whatever membership
    row exists, so the script's write would win. These tests pin the guard.
    """

    def setUp(self):
        from scripts import backfill_companion_access as backfill
        self.backfill = backfill

    def test_voice_agent_is_on_the_never_backfill_list(self):
        self.assertIn("voice-agent", self.backfill.NEVER_BACKFILL)

    def test_a_plan_to_grant_voice_is_refused(self):
        plan = [("u1", "o1", "mail-agent", "voice-agent")]
        allowed, refused = self.backfill._permitted(plan)
        self.assertEqual(allowed, [])
        self.assertEqual(refused, plan)

    def test_the_reverse_direction_is_still_allowed(self):
        # Granting Email to a Voice-only user stays consistent with the model:
        # Email entitlement is the Agents app role, which they already hold.
        plan = [("u1", "o1", "voice-agent", "mail-agent")]
        allowed, refused = self.backfill._permitted(plan)
        self.assertEqual(allowed, plan)
        self.assertEqual(refused, [])

    def test_a_mixed_plan_is_split_and_nothing_is_lost(self):
        to_voice = ("u1", "o1", "mail-agent", "voice-agent")
        to_mail = ("u2", "o1", "voice-agent", "mail-agent")
        allowed, refused = self.backfill._permitted([to_voice, to_mail])
        self.assertEqual(allowed, [to_mail])
        self.assertEqual(refused, [to_voice])

    def test_write_site_refuses_voice_even_if_the_plan_was_not_filtered(self):
        # Defence in depth: the guard immediately before the INSERT, reached only if
        # a future edit bypasses `_permitted`. Asserted on the source because the
        # surrounding loop needs a live database to execute.
        import inspect
        src = inspect.getsource(self.backfill.run)
        self.assertIn("NEVER_BACKFILL", src)
        self.assertIn("raise SystemExit", src)
        guard_at = src.index("if need in NEVER_BACKFILL")
        insert_at = src.index("session.add(Membership(")
        self.assertLess(guard_at, insert_at, "the guard must precede the INSERT")

    def test_script_is_preserved_rather_than_disabled(self):
        # The ticket asked for it guarded, not removed: the dry-run inventory and
        # the voice->mail direction both still work.
        self.assertTrue(callable(self.backfill.run))
        self.assertEqual(
            self.backfill.COMPANION,
            {"mail-agent": "voice-agent", "voice-agent": "mail-agent"},
        )


if __name__ == "__main__":
    unittest.main()
