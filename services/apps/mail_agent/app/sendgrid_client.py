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


def send_reply(*, username: str, to_addr: str, subject: str, body: str, **_ignored_send_creds) -> None:
    """`username` is the configured mailbox's own address (the same value the
    IMAP read client logs in with) — replies send "from" this, same as raw SMTP
    always did, so switching the configured mailbox to a different address
    keeps sending as that address rather than some fixed, unrelated sender.

    SendGrid requires `username` to be a Single Sender verified in its
    dashboard, or it rejects the send with 403 — so reconfiguring the mailbox
    to a new address also means verifying that new address in SendGrid, or
    auto-send starts failing (safely — falls back to a draft, same as any
    other send failure) until it's verified.

    `_ignored_send_creds` absorbs smtp_host/smtp_port/password, which this
    doesn't need — `send_creds` is shared with the raw-SMTP send path, and
    callers pass it through unchanged regardless of which one ends up used."""
    if not settings.sendgrid_api_key:
        raise RuntimeError("SENDGRID_API_KEY not configured")
    resp = requests.post(
        SENDGRID_API_URL,
        headers={
            "Authorization": f"Bearer {settings.sendgrid_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "personalizations": [{"to": [{"email": to_addr}]}],
            "from": {"email": username},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        },
        timeout=20,
    )
    resp.raise_for_status()
