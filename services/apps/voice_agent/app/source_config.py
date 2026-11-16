"""The recording source, resolved from deployment configuration.

Project West points the agent at one phone system once, in the environment, and it
does not change at runtime. This module is the only place those variables are read,
so every caller — the pipeline, the scheduler, the audio route — agrees on what is
configured and on whether it is configured at all.

Why this replaced the `voice_settings` row
------------------------------------------
Credentials used to live in the database, written by a settings form. That had two
problems beyond the obvious one of storing secrets in a table:

  * **"Configured" was ambiguous.** A row existed with `enabled = true` the moment
    anyone loaded the dashboard, because the settings reader was find-or-create. It
    carried no credentials, but it satisfied the only check the manual sync route
    made — so pressing "Sync now" on a fresh database fell through to BT Cloud's
    demo fallback and filled it with fabricated recordings. `configured` below is
    about credentials, not about a row existing.
  * **It could disagree with the deployment.** Whatever was typed into the form won,
    even where it contradicted the environment the service was actually running in.

Pure: no I/O, no database, no session. The one decision that determines whether the
agent talks to a real phone system is therefore also the cheapest thing here to test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import settings

BT_CLOUD = "bt_cloud"
S3 = "s3"


@dataclass(frozen=True)
class SourceConfig:
    """What the recording source is, and whether it is usable."""

    source_type: str
    #: Credentials in exactly the shape `bt_client`/`s3_client` take as kwargs.
    #: Never logged, never serialised to a response.
    creds: dict[str, Any] = field(default_factory=dict)
    #: False when a required credential is missing. The agent must refuse to sync
    #: rather than fall back to demo data, which would otherwise be indistinguishable
    #: from real recordings once stored.
    configured: bool = False
    #: Why it is unusable, for the log and the API's error message. Empty when fine.
    reason: str = ""
    cron_frequency: str = "every6h"
    cron_time: str = "02:00"

    @property
    def transcribe_creds(self) -> dict[str, Any]:
        """Credentials for the per-recording download+transcribe call.

        S3's lister takes a `prefix` that its object fetcher does not, so it is
        dropped here rather than at each call site.
        """
        if self.source_type == S3:
            return {k: v for k, v in self.creds.items() if k != "prefix"}
        return dict(self.creds)

    def describe(self) -> str:
        """A short, safe description for logs and operator-facing messages.

        Names what is configured, never a credential — not even partially. A
        truncated secret in a log is still a secret in a log.
        """
        if self.source_type == S3:
            where = self.creds.get("bucket") or "(no bucket)"
            return f"S3-compatible bucket {where}"
        where = self.creds.get("endpoint") or "(no server URL)"
        return f"BT Cloud / RingCentral at {where}"


def load() -> SourceConfig:
    """Resolve the configured recording source. Never raises."""
    source_type = (settings.voice_source_type or BT_CLOUD).strip().lower()
    common = {
        "cron_frequency": (settings.voice_cron_frequency or "every6h").strip(),
        "cron_time": (settings.voice_cron_time or "02:00").strip(),
    }

    if source_type == S3:
        creds = {
            "endpoint": (settings.s3_endpoint or "").strip(),
            "region": (settings.s3_region or "").strip(),
            "bucket": (settings.s3_bucket or "").strip(),
            "prefix": (settings.s3_prefix or "").strip(),
            "access_key_id": (settings.s3_access_key_id or "").strip(),
            "secret_access_key": settings.s3_secret_access_key or "",
        }
        missing = [
            name
            for name, key in (
                ("S3_BUCKET", "bucket"),
                ("S3_ACCESS_KEY_ID", "access_key_id"),
                ("S3_SECRET_ACCESS_KEY", "secret_access_key"),
            )
            if not creds[key]
        ]
        return SourceConfig(
            source_type=S3,
            creds=creds,
            configured=not missing,
            reason="" if not missing else "not configured: " + ", ".join(missing) + " unset",
            **common,
        )

    if source_type != BT_CLOUD:
        return SourceConfig(
            source_type=source_type,
            configured=False,
            reason=f"unknown VOICE_SOURCE_TYPE {source_type!r}; expected 'bt_cloud' or 's3'",
            **common,
        )

    creds = {
        "endpoint": (settings.rc_server_url or "").strip(),
        "client_id": (settings.rc_client_id or "").strip(),
        "client_secret": settings.rc_client_secret or "",
        "jwt": settings.rc_jwt or "",
    }
    # All four are required together. A partial set is the dangerous case: with a
    # server URL but no JWT, `bt_client` serves demo recordings rather than failing,
    # so a half-configured deployment would quietly produce fabricated data.
    missing = [
        name
        for name, key in (
            ("RC_SERVER_URL", "endpoint"),
            ("RC_CLIENT_ID", "client_id"),
            ("RC_CLIENT_SECRET", "client_secret"),
            ("RC_JWT", "jwt"),
        )
        if not creds[key]
    ]
    return SourceConfig(
        source_type=BT_CLOUD,
        creds=creds,
        configured=not missing,
        reason="" if not missing else "not configured: " + ", ".join(missing) + " unset",
        **common,
    )


def public_view() -> dict[str, Any]:
    """The configuration as an operator may see it. No secrets, ever.

    Secrets are reported as booleans so "is the JWT set?" is answerable without the
    value being exposed to a browser.
    """
    cfg = load()
    out: dict[str, Any] = {
        "sourceType": cfg.source_type,
        "configured": cfg.configured,
        "reason": cfg.reason,
        "cronFrequency": cfg.cron_frequency,
        "cronTime": cfg.cron_time,
        "managedBy": "environment",
    }
    if cfg.source_type == S3:
        out |= {
            "s3Endpoint": cfg.creds.get("endpoint", ""),
            "s3Region": cfg.creds.get("region", ""),
            "s3Bucket": cfg.creds.get("bucket", ""),
            "s3Prefix": cfg.creds.get("prefix", ""),
            "s3AccessKeyId": cfg.creds.get("access_key_id", ""),
            "s3SecretConfigured": bool(cfg.creds.get("secret_access_key")),
        }
    else:
        out |= {
            "endpoint": cfg.creds.get("endpoint", ""),
            "clientId": cfg.creds.get("client_id", ""),
            "secretConfigured": bool(cfg.creds.get("client_secret")),
            "jwtConfigured": bool(cfg.creds.get("jwt")),
        }
    return out
