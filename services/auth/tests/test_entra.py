"""Entra access-token verification (app/entra.py).

Run from backend/services/auth with the venv active:
    python -m unittest discover -s tests

Tokens are signed with a throwaway RSA key and the JWKS lookup is stubbed, so
nothing here talks to Microsoft.
"""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app import entra
from app.config import settings

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(effective_age: float = 5, key=KEY, **overrides) -> str:
    """A v2 Entra access token issued `effective_age` seconds ago — with iat/nbf
    backdated 5 minutes, as Entra does."""
    iat = int(time.time() - effective_age - entra._ENTRA_IAT_BACKDATE)
    claims = {
        "iss": ISSUER,
        "aud": CLIENT,
        "tid": TENANT,
        "oid": "33333333-3333-3333-3333-333333333333",
        "scp": "access_as_user",
        "iat": iat,
        "nbf": iat,
        "exp": iat + 3900,
    }
    claims.update(overrides)
    for name in [k for k, v in claims.items() if v is None]:
        del claims[name]
    return jwt.encode(claims, key, algorithm="RS256")


class VerifyTests(unittest.TestCase):
    def setUp(self):
        jwks = SimpleNamespace(get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=KEY.public_key()))
        for patcher in (
            mock.patch.object(entra, "_jwks", return_value=jwks),
            mock.patch.object(settings, "entra_tenant_id", TENANT),
            mock.patch.object(settings, "entra_client_id", CLIENT),
            mock.patch.object(settings, "entra_scope", "access_as_user"),
            mock.patch.object(settings, "entra_max_token_age", 300),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def assertRejected(self, token: str, message: str):
        with self.assertRaises(entra.EntraTokenError) as ctx:
            entra._verify(token)
        self.assertIn(message, str(ctx.exception))

    # --- freshness ------------------------------------------------------------

    def test_freshly_issued_token_is_accepted(self):
        # Apparent iat age ≈ 302 s, which the old check rejected as "too old".
        self.assertEqual(entra._verify(_token(effective_age=2))["tid"], TENANT)

    def test_token_under_max_age_is_accepted(self):
        entra._verify(_token(effective_age=290))

    def test_token_over_max_age_is_rejected(self):
        self.assertRejected(_token(effective_age=310), "token is too old")

    def test_max_age_setting_still_applies(self):
        with mock.patch.object(settings, "entra_max_token_age", 60):
            self.assertRejected(_token(effective_age=90), "token is too old")

    # --- unchanged checks -----------------------------------------------------

    def test_wrong_signature_is_rejected(self):
        self.assertRejected(_token(key=OTHER_KEY), "invalid Microsoft token")

    def test_wrong_issuer_is_rejected(self):
        self.assertRejected(_token(iss=f"https://sts.windows.net/{TENANT}/"), "invalid Microsoft token")

    def test_wrong_audience_is_rejected(self):
        self.assertRejected(_token(aud="api://someone-else"), "invalid Microsoft token")

    def test_wrong_tenant_is_rejected(self):
        self.assertRejected(_token(tid="44444444-4444-4444-4444-444444444444"), "different tenant")

    def test_missing_scope_is_rejected(self):
        self.assertRejected(_token(scp="User.Read"), "missing the access_as_user scope")

    def test_missing_scp_claim_is_rejected(self):
        self.assertRejected(_token(scp=None), "invalid Microsoft token")

    def test_expired_token_is_rejected(self):
        self.assertRejected(_token(exp=int(time.time()) - 120), "invalid Microsoft token")

    def test_not_yet_valid_token_is_rejected(self):
        self.assertRejected(_token(nbf=int(time.time()) + 120), "invalid Microsoft token")

    def test_unconfigured_server_rejects(self):
        with mock.patch.object(settings, "entra_client_id", None):
            self.assertRejected(_token(), "not configured")


if __name__ == "__main__":
    unittest.main()
