"""SendGrid HTTP API mail sending — the replacement for raw SMTP on platforms
that don't route outbound SMTP traffic (confirmed live on Railway: connecting
to smtp.gmail.com:587 fails immediately with "[Errno 101] Network is
unreachable", not a credentials or DNS problem — the network path itself is
blocked). SendGrid's API is plain HTTPS, unaffected by that.

Sync (like graph_client.send_reply) — pipeline.py/api.py call this via
run_in_threadpool, never directly in an async context.
"""

from __future__ import annotations

import requests

from .config import settings

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"


def send_reply(*, to_addr: str, subject: str, body: str, **_ignored_send_creds) -> None:
    """`_ignored_send_creds` absorbs whatever the caller's `send_creds` dict for
    this org happens to carry (smtp_host/smtp_port/username/password for an
    IMAP mailbox) — this function needs none of that, it authenticates with
    `settings.sendgrid_api_key` instead, so callers don't need special-casing
    to swap send functions."""
    if not settings.sendgrid_api_key or not settings.sendgrid_from_email:
        raise RuntimeError("SENDGRID_API_KEY / SENDGRID_FROM_EMAIL not configured")
    resp = requests.post(
        SENDGRID_API_URL,
        headers={
            "Authorization": f"Bearer {settings.sendgrid_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "personalizations": [{"to": [{"email": to_addr}]}],
            "from": {"email": settings.sendgrid_from_email},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        },
        timeout=20,
    )
    resp.raise_for_status()
