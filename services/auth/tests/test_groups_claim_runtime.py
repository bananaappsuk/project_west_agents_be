"""End-to-end handling of the Entra `groups` claim, through the real code path.

Why this exists
---------------
The Entra side has been verified visually: PW Agents (Staging) -> Token
configuration -> groups -> **Access** enabled, format **Group ID**, manifest
`"groupMembershipClaims": "SecurityGroup"`. What that confirms is what Microsoft
will *send*. It does not confirm what this service does with it.

Runtime observation is not available from the repository: `configure_logging` uses
`logging.basicConfig` with no file handler, and `run-all.ps1` starts each service in
its own console with no redirection, so auth logs live in a window's scrollback and
are never written to disk. There is therefore no historical sign-in to inspect.

So this test substitutes the one thing that can be checked without a live tenant: a
token shaped exactly as that configuration produces -- `groups` as an array of group
object ids -- driven through the **real** `verify_access_token`, the **real**
`_entra_groups`, and the **real** `_entitled_apps`. Nothing in the chain is stubbed
except the JWKS lookup, which is replaced with a throwaway key so no network call
and no real token is involved.

What it proves: given a correctly configured token, PW-Voice-Users is recognised and
Voice is granted. What it cannot prove: that Entra is in fact sending the claim to
this particular registration. Only a real sign-in shows that -- see the verification
steps in the Phase 1 follow-up report.

No real token, refresh token, client secret or signing key is used or printed. The
group ids below are directory object ids, not credentials.
"""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app import entra as entra_verify
from app.config import settings
from app.routers import entra

# The live Project West values, as recorded in services/auth/.env.example and
# confirmed by PW. Identifiers, not secrets.
TENANT = "98b19873-116e-481c-8f5b-fd536f44f02c"
CLIENT = "49dc5530-53c6-4cbc-8c90-8f7686cf02e3"
VOICE_GROUP = "9245c941-a3f1-46d1-a225-1d20ba7dde8b"

# Three of the six mailbox groups seeded by the mail agent
# (migrate_pw_seed_access_rules.sql), so the token under test looks like a real
# staff member's rather than a single-group special case. Under
# groupMembershipClaims=SecurityGroup these arrive alongside the voice group.
MAILBOX_GROUPS = [
    "2c5a11c6-7f9c-4ba1-abf7-5ca143547b10",  # PW-Mailbox-Info
    "d5347ccb-9793-4025-9988-e148e3af2a4c",  # PW-Mailbox-Referrals
    "82c9db9a-d965-41ea-9e85-3c64152ed12b",  # PW-Mailbox-Coordinator
]

ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
OID = "33333333-3333-3333-3333-333333333333"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(**overrides) -> str:
    """A v2 Entra access token for this API, as PW Agents (Staging) issues it.

    `iat`/`nbf` are backdated 5 minutes exactly as Entra does, so the freshness
    check in `_verify` sees a just-issued token.
    """
    iat = int(time.time() - 2 - entra_verify._ENTRA_IAT_BACKDATE)
    claims = {
        "iss": ISSUER,
        "aud": CLIENT,
        "tid": TENANT,
        "oid": OID,
        "upn": "test.user@projectwest.co.uk",
        "scp": "access_as_user",
        "roles": ["Agents.User"],
        "iat": iat,
        "nbf": iat,
        "exp": iat + 3900,
    }
    claims.update(overrides)
    for name in [k for k, v in claims.items() if v is None]:
        del claims[name]
    return jwt.encode(claims, KEY, algorithm="RS256")


class GroupsClaimEndToEndTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        jwks = SimpleNamespace(
            get_signing_key_from_jwt=lambda _t: SimpleNamespace(key=KEY.public_key())
        )
        for patcher in (
            mock.patch.object(entra_verify, "_jwks", return_value=jwks),
            mock.patch.object(settings, "entra_tenant_id", TENANT),
            mock.patch.object(settings, "entra_client_id", CLIENT),
            mock.patch.object(settings, "entra_scope", "access_as_user"),
            mock.patch.object(settings, "entra_max_token_age", 300),
            mock.patch.object(settings, "entra_voice_group_id", VOICE_GROUP),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _decide(self, token: str):
        """Verify a token and resolve entitlement, exactly as the handler does."""
        claims = await entra_verify.verify_access_token(token)
        role = entra._display_role(claims.get("roles") or [])
        groups = entra._entra_groups(claims)
        return claims, groups, entra._entitled_apps(role=role, groups=groups, oid=claims.get("oid"))

    # --- the configuration PW verified, end to end ---------------------------

    async def test_security_group_claim_grants_voice(self):
        """groupMembershipClaims=SecurityGroup, format=Group ID, Access enabled."""
        token = _token(groups=[*MAILBOX_GROUPS, VOICE_GROUP])
        claims, groups, apps = await self._decide(token)

        self.assertIn("groups", claims, "the claim must survive verification")
        self.assertEqual(len(groups), 4, "all four group ids are read, not just one")
        self.assertIn(VOICE_GROUP, groups)
        self.assertEqual(apps, ("mail-agent", "voice-agent"))

    async def test_mailbox_groups_reach_the_token_unchanged(self):
        """The mail agent resolves mailbox access from these same ids, so the voice
        gate must not filter the claim down to what it cares about."""
        token = _token(groups=[*MAILBOX_GROUPS, VOICE_GROUP])
        _, groups, _ = await self._decide(token)
        for g in MAILBOX_GROUPS:
            self.assertIn(g, groups)

    async def test_claim_order_does_not_matter(self):
        token = _token(groups=[VOICE_GROUP, *MAILBOX_GROUPS])
        _, _, apps = await self._decide(token)
        self.assertIn("voice-agent", apps)

    async def test_voice_group_is_matched_on_object_id_not_name(self):
        # Entra sends object ids under format=Group ID. A display name must not match.
        token = _token(groups=["PW-Voice-Users"])
        _, _, apps = await self._decide(token)
        self.assertNotIn("voice-agent", apps)

    # --- a staff member who is not a Voice user ------------------------------

    async def test_non_member_with_mailbox_groups_gets_email_only(self):
        token = _token(groups=MAILBOX_GROUPS)
        _, groups, apps = await self._decide(token)
        self.assertEqual(len(groups), 3)
        self.assertEqual(apps, ("mail-agent",))

    # --- the misconfigurations this is meant to catch ------------------------

    async def test_access_token_without_the_claim_denies_voice(self):
        """The 'ticked ID token but not Access' case: everything else verifies."""
        token = _token(groups=None)
        claims, groups, apps = await self._decide(token)
        self.assertNotIn("groups", claims)
        self.assertEqual(groups, [])
        self.assertEqual(apps, ("mail-agent",), "fails closed, and Email is unaffected")

    async def test_group_overage_denies_voice(self):
        """Entra replaces `groups` with `_claim_names`/`_claim_sources` past ~200
        groups. SecurityGroup (rather than ApplicationGroup) makes this reachable in
        principle, so the behaviour is pinned rather than assumed."""
        token = _token(
            groups=None,
            _claim_names={"groups": "src1"},
            _claim_sources={"src1": {"endpoint": "https://graph.microsoft.com/..."}},
        )
        _, groups, apps = await self._decide(token)
        self.assertEqual(groups, [])
        self.assertNotIn("voice-agent", apps)

    async def test_unconfigured_group_id_denies_voice_even_with_a_valid_claim(self):
        token = _token(groups=[VOICE_GROUP])
        with mock.patch.object(settings, "entra_voice_group_id", None):
            _, _, apps = await self._decide(token)
        self.assertEqual(apps, ("mail-agent",))

    async def test_a_token_for_a_different_registration_is_rejected_outright(self):
        """If ENTRA_CLIENT_ID does not match the registration the token was minted
        for, this fails at verification rather than silently losing the claim."""
        token = _token(aud="00000000-0000-0000-0000-000000000000", groups=[VOICE_GROUP])
        with self.assertRaises(entra_verify.EntraTokenError):
            await self._decide(token)

    # --- the audit line an operator will actually read ----------------------

    async def test_group_count_is_logged_without_logging_the_ids(self):
        """`count=N` is what distinguishes 'claim absent' from 'claim present but no
        voice group'. The ids themselves are deliberately not logged."""
        token = _token(groups=[*MAILBOX_GROUPS, VOICE_GROUP])
        with self.assertLogs("auth.entra", level="INFO") as captured:
            await self._decide(token)
        line = next(m for m in captured.output if "AUDIT entra groups" in m)
        self.assertIn("count=4", line)
        self.assertNotIn(VOICE_GROUP, line)

    async def test_denial_reason_is_distinguishable_in_the_log(self):
        with self.assertLogs("auth.entra", level="INFO") as captured:
            await self._decide(_token(groups=MAILBOX_GROUPS))
        self.assertTrue(any("reason=not_in_group" in m for m in captured.output))

        with self.assertLogs("auth.entra", level="WARNING") as captured:
            await self._decide(_token(groups=None))
        self.assertTrue(any("reason=no_groups_claim" in m for m in captured.output))


if __name__ == "__main__":
    unittest.main()
