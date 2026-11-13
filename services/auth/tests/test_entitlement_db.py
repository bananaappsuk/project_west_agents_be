"""Per-application entitlement against a REAL database session.

`test_entitlement.py` covers the rule and the handler's wiring with a stub session.
What it cannot cover is what the database itself does: that creating a membership
really inserts one, that `_revoke_app_access` really deletes one, and that deleting
a membership takes its `membership_roles` association rows with it via ON DELETE
CASCADE. Those were the open gap (risk R5 in the Phase 1 report).

Skipped unless `aiosqlite` is importable, so the committed suite still runs clean in
the service venv, which does not have it. Run it in an isolated environment instead
-- production dependencies are deliberately NOT changed:

    python -m pip install --target <scratch>/testlibs aiosqlite
    $env:PYTHONPATH = "<scratch>/testlibs"
    .\.venv\Scripts\python.exe -m unittest discover -s tests

Two things this harness has to fake, both noted because they bound what it proves:

  * `User.entra_groups` is `JSONB`, which the SQLite dialect cannot render. A
    test-only `@compiles` shim maps it to `JSON` for SQLite. It changes nothing in
    the models and nothing in any other dialect.
  * SQLite ignores foreign keys unless asked, so `PRAGMA foreign_keys=ON` is set on
    every connection. Without it the cascade assertion below would pass
    vacuously -- Postgres enforces this always, SQLite only on request.
"""

from __future__ import annotations

import unittest

try:  # pragma: no cover - the import IS the check
    import aiosqlite  # noqa: F401
    HAVE_AIOSQLITE = True
except ModuleNotFoundError:
    HAVE_AIOSQLITE = False

if HAVE_AIOSQLITE:
    import asyncio
    from unittest import mock

    from sqlalchemy import event, func, select, text
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.pool import StaticPool

    from app.config import settings
    from app.models import Application, Base, Membership, Organization, RefreshToken, User
    from app.routers import entra

    @compiles(JSONB, "sqlite")
    def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001
        return "JSON"


MAIL = "mail-agent"
VOICE = "voice-agent"
OID = "33333333-3333-3333-3333-333333333333"
VOICE_GROUP = "9245c941-a3f1-46d1-a225-1d20ba7dde8b"
OTHER_GROUP = "bbbbbbbb-5555-6666-7777-888888888888"


@unittest.skipUnless(HAVE_AIOSQLITE, "aiosqlite not installed (isolated test env only)")
class EntitlementDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,  # one connection, so :memory: survives between sessions
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def _fk_on(dbapi_conn, _record):  # noqa: ANN001
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self._run(self._schema())

        patcher = mock.patch.object(settings, "entra_voice_group_id", VOICE_GROUP)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: self._run(self.engine.dispose()))

    @staticmethod
    def _run(coro):
        return asyncio.run(coro)

    async def _schema(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.Session() as session:
            session.add(Organization(id="org-1", name="Project West", slug="pw"))
            session.add(User(id="user-1", email="a@projectwest.co.uk", entra_oid=OID, is_active=True))
            await session.commit()

    async def _sign_in(self, groups):
        claims = {
            "oid": OID, "tid": "t", "upn": "a@projectwest.co.uk",
            "email": "a@projectwest.co.uk", "name": "A",
            "groups": list(groups), "roles": ["Agents.User"],
        }

        async def verify(_token):
            return claims

        with mock.patch.object(entra, "verify_access_token", verify):
            async with self.Session() as session:
                return await entra.entra_sign_in(authorization="Bearer x", session=session)

    async def _membership_keys(self):
        async with self.Session() as session:
            rows = await session.execute(
                select(Application.key)
                .join(Membership, Membership.app_id == Application.id)
                .where(Membership.user_id == "user-1")
            )
            return sorted(r[0] for r in rows)

    async def _association_row_count(self):
        async with self.Session() as session:
            return (await session.execute(text("SELECT count(*) FROM membership_roles"))).scalar()

    async def _live_refresh_app_ids(self):
        async with self.Session() as session:
            rows = await session.execute(
                select(Application.key)
                .join(RefreshToken, RefreshToken.app_id == Application.id)
                .where(RefreshToken.user_id == "user-1", RefreshToken.revoked.is_(False))
            )
            return sorted(r[0] for r in rows)

    # --- real inserts ---------------------------------------------------------

    def test_voice_group_member_gets_both_memberships_persisted(self):
        out = self._run(self._sign_in([VOICE_GROUP]))
        self.assertEqual(sorted(out.tokens), [MAIL, VOICE])
        self.assertEqual(self._run(self._membership_keys()), [MAIL, VOICE])

    def test_non_member_gets_only_an_email_membership_persisted(self):
        out = self._run(self._sign_in([OTHER_GROUP]))
        self.assertEqual(sorted(out.tokens), [MAIL])
        self.assertEqual(self._run(self._membership_keys()), [MAIL])

    def test_no_voice_refresh_token_row_is_created_for_a_non_member(self):
        self._run(self._sign_in([OTHER_GROUP]))
        self.assertEqual(self._run(self._live_refresh_app_ids()), [MAIL])

    # --- real revocation, and the cascade R5 flagged --------------------------

    def test_losing_group_membership_deletes_the_voice_membership(self):
        self._run(self._sign_in([VOICE_GROUP]))
        self.assertEqual(self._run(self._membership_keys()), [MAIL, VOICE])

        # Removed from PW-Voice-Users in Entra, then signs in again.
        self._run(self._sign_in([OTHER_GROUP]))
        self.assertEqual(self._run(self._membership_keys()), [MAIL])

    def test_deleting_a_membership_cascades_to_its_role_associations(self):
        # The gap the stub session could not reach: no orphan membership_roles row
        # is left pointing at a membership that no longer exists.
        self._run(self._sign_in([VOICE_GROUP]))
        both = self._run(self._association_row_count())
        self.assertEqual(both, 2, "expected one role association per membership")

        self._run(self._sign_in([OTHER_GROUP]))
        self.assertEqual(self._run(self._association_row_count()), 1)

    def test_losing_group_membership_revokes_voice_refresh_tokens(self):
        self._run(self._sign_in([VOICE_GROUP]))
        self.assertEqual(self._run(self._live_refresh_app_ids()), [MAIL, VOICE])

        self._run(self._sign_in([OTHER_GROUP]))
        # Email keeps a live token; Voice has none left to refresh from.
        self.assertEqual(self._run(self._live_refresh_app_ids()), [MAIL, MAIL])

    def test_email_membership_is_untouched_across_a_voice_revocation(self):
        self._run(self._sign_in([VOICE_GROUP]))
        self._run(self._sign_in([OTHER_GROUP]))

        async def mail_membership_count():
            async with self.Session() as session:
                return await session.scalar(
                    select(func.count())
                    .select_from(Membership)
                    .join(Application, Application.id == Membership.app_id)
                    .where(Membership.user_id == "user-1", Application.key == MAIL)
                )

        self.assertEqual(self._run(mail_membership_count()), 1)

    def test_regaining_group_membership_restores_voice_with_no_db_work(self):
        self._run(self._sign_in([VOICE_GROUP]))
        self._run(self._sign_in([OTHER_GROUP]))
        self.assertEqual(self._run(self._membership_keys()), [MAIL])

        # Added back to PW-Voice-Users. Next sign-in is the whole re-grant.
        out = self._run(self._sign_in([VOICE_GROUP]))
        self.assertEqual(sorted(out.tokens), [MAIL, VOICE])
        self.assertEqual(self._run(self._membership_keys()), [MAIL, VOICE])


if __name__ == "__main__":
    unittest.main()
