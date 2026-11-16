"""The recording source, resolved from deployment configuration.

Run from backend/services/apps/voice_agent with the venv active:
    python -m unittest discover -s tests

`source_config.load()` is pure, so every branch is reachable without a database,
a network call or a phone system. The branch that matters most is the partial
one: with a server URL but no JWT, `bt_client` serves DEMO recordings rather than
failing, so a half-configured deployment would quietly store fabricated calls that
are indistinguishable from real ones once written. `configured` has to be false
for every incomplete combination, not just the empty one.
"""

from __future__ import annotations

import unittest
from unittest import mock

from app import source_config
from app.config import settings

RC = {
    "rc_server_url": "https://platform.ringcentral.com",
    "rc_client_id": "client-123",
    "rc_client_secret": "secret-123",
    "rc_jwt": "jwt-123",
}
S3 = {
    "s3_bucket": "pw-recordings",
    "s3_access_key_id": "AKIA-test",
    "s3_secret_access_key": "s3-secret",
    "s3_region": "eu-west-2",
    "s3_prefix": "calls/",
    "s3_endpoint": "",
}

#: Every source field, so a test starts from "nothing configured" rather than
#: inheriting whatever the developer's own .env happens to hold.
BLANK = dict.fromkeys([*RC, *S3], "")


def configure(**overrides):
    values = {**BLANK, "voice_source_type": "bt_cloud",
              "voice_cron_frequency": "every6h", "voice_cron_time": "02:00"}
    values.update(overrides)
    return mock.patch.multiple(settings, **values)


class BtCloudTests(unittest.TestCase):
    def test_all_four_values_present_is_configured(self):
        with configure(**RC):
            cfg = source_config.load()
        self.assertTrue(cfg.configured)
        self.assertEqual(cfg.reason, "")
        self.assertEqual(cfg.source_type, "bt_cloud")

    def test_creds_match_the_kwargs_bt_client_takes(self):
        with configure(**RC):
            cfg = source_config.load()
        self.assertEqual(
            set(cfg.creds), {"endpoint", "client_id", "client_secret", "jwt"}
        )
        self.assertEqual(cfg.creds["endpoint"], RC["rc_server_url"])

    def test_nothing_set_is_not_configured(self):
        with configure():
            cfg = source_config.load()
        self.assertFalse(cfg.configured)
        for name in ("RC_SERVER_URL", "RC_CLIENT_ID", "RC_CLIENT_SECRET", "RC_JWT"):
            self.assertIn(name, cfg.reason)

    def test_each_missing_value_alone_is_enough_to_refuse(self):
        for key, name in (
            ("rc_server_url", "RC_SERVER_URL"),
            ("rc_client_id", "RC_CLIENT_ID"),
            ("rc_client_secret", "RC_CLIENT_SECRET"),
            ("rc_jwt", "RC_JWT"),
        ):
            with self.subTest(missing=name):
                with configure(**{**RC, key: ""}):
                    cfg = source_config.load()
                self.assertFalse(cfg.configured)
                self.assertIn(name, cfg.reason)

    def test_server_url_without_a_jwt_is_refused(self):
        # The dangerous partial case: bt_client would fall back to demo recordings.
        with configure(rc_server_url=RC["rc_server_url"], rc_client_id="x"):
            cfg = source_config.load()
        self.assertFalse(cfg.configured)
        self.assertIn("RC_JWT", cfg.reason)

    def test_whitespace_only_is_treated_as_unset(self):
        with configure(**{**RC, "rc_server_url": "   "}):
            cfg = source_config.load()
        self.assertFalse(cfg.configured)

    def test_surrounding_whitespace_is_trimmed_from_a_real_value(self):
        # Pasted from a dashboard, these routinely carry a trailing space.
        with configure(**{**RC, "rc_server_url": "  https://platform.ringcentral.com  "}):
            cfg = source_config.load()
        self.assertTrue(cfg.configured)
        self.assertEqual(cfg.creds["endpoint"], "https://platform.ringcentral.com")

    def test_transcribe_creds_are_unchanged_for_bt_cloud(self):
        with configure(**RC):
            cfg = source_config.load()
        self.assertEqual(cfg.transcribe_creds, cfg.creds)


class S3Tests(unittest.TestCase):
    def test_configured_when_bucket_and_keys_are_present(self):
        with configure(voice_source_type="s3", **S3):
            cfg = source_config.load()
        self.assertTrue(cfg.configured)
        self.assertEqual(cfg.source_type, "s3")

    def test_endpoint_may_be_blank_for_aws(self):
        with configure(voice_source_type="s3", **{**S3, "s3_endpoint": ""}):
            cfg = source_config.load()
        self.assertTrue(cfg.configured, "a blank endpoint means AWS, not unconfigured")

    def test_prefix_is_optional(self):
        with configure(voice_source_type="s3", **{**S3, "s3_prefix": ""}):
            self.assertTrue(source_config.load().configured)

    def test_missing_bucket_or_keys_is_refused(self):
        for key, name in (
            ("s3_bucket", "S3_BUCKET"),
            ("s3_access_key_id", "S3_ACCESS_KEY_ID"),
            ("s3_secret_access_key", "S3_SECRET_ACCESS_KEY"),
        ):
            with self.subTest(missing=name):
                with configure(voice_source_type="s3", **{**S3, key: ""}):
                    cfg = source_config.load()
                self.assertFalse(cfg.configured)
                self.assertIn(name, cfg.reason)

    def test_transcribe_creds_drop_prefix(self):
        # s3_client.download/transcribe_one take no `prefix`; only the lister does.
        with configure(voice_source_type="s3", **S3):
            cfg = source_config.load()
        self.assertIn("prefix", cfg.creds)
        self.assertNotIn("prefix", cfg.transcribe_creds)


class SourceTypeTests(unittest.TestCase):
    def test_unknown_source_type_is_refused_rather_than_guessed(self):
        with configure(voice_source_type="ringcentral", **RC):
            cfg = source_config.load()
        self.assertFalse(cfg.configured)
        self.assertIn("unknown VOICE_SOURCE_TYPE", cfg.reason)

    def test_source_type_is_case_insensitive(self):
        with configure(voice_source_type="S3", **S3):
            self.assertTrue(source_config.load().configured)

    def test_blank_source_type_defaults_to_bt_cloud(self):
        with configure(voice_source_type="", **RC):
            cfg = source_config.load()
        self.assertEqual(cfg.source_type, "bt_cloud")
        self.assertTrue(cfg.configured)


class PublicViewTests(unittest.TestCase):
    """What an operator may see. The whole point is that it carries no secret."""

    def test_secrets_are_reported_as_booleans_never_values(self):
        with configure(**RC):
            view = source_config.public_view()
        blob = repr(view)
        self.assertNotIn(RC["rc_client_secret"], blob)
        self.assertNotIn(RC["rc_jwt"], blob)
        self.assertTrue(view["secretConfigured"])
        self.assertTrue(view["jwtConfigured"])

    def test_s3_secret_is_not_exposed(self):
        with configure(voice_source_type="s3", **S3):
            view = source_config.public_view()
        self.assertNotIn(S3["s3_secret_access_key"], repr(view))
        self.assertTrue(view["s3SecretConfigured"])

    def test_reports_what_is_wrong_when_unconfigured(self):
        with configure():
            view = source_config.public_view()
        self.assertFalse(view["configured"])
        self.assertIn("RC_SERVER_URL", view["reason"])

    def test_says_it_is_managed_by_the_environment(self):
        with configure(**RC):
            self.assertEqual(source_config.public_view()["managedBy"], "environment")

    def test_describe_names_the_target_without_a_credential(self):
        with configure(**RC):
            text = source_config.load().describe()
        self.assertIn("platform.ringcentral.com", text)
        self.assertNotIn(RC["rc_jwt"], text)
        self.assertNotIn(RC["rc_client_secret"], text)


if __name__ == "__main__":
    unittest.main()
