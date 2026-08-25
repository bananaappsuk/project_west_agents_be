"""Microsoft Graph mail fetch + send (app-only / client-credentials). Sync — call via run_in_threadpool.

Alternative to mail_client's IMAP fetch and SMTP send for orgs whose tenant has
IMAP Basic Auth disabled but has an Entra app registration with Mail.Read and
Mail.Send (application permissions, admin-consented). Selecting Graph as the
read provider covers both directions — there is no separate SMTP step.
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

import msal
import requests


class FetchResult(NamedTuple):
    messages: list[dict]
    # The most recent receivedDateTime this batch scanned (whether or not that
    # particular message ended up in `messages` — e.g. already-known ones are
    # skipped but still count) — the caller advances its sync watermark to this.
    max_received_at: datetime | None

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPE = ["https://graph.microsoft.com/.default"]


def _get_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    app = msal.ConfidentialClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        client_credential=client_secret,
    )
    result = app.acquire_token_for_client(scopes=SCOPE)
    if "access_token" not in result:
        raise RuntimeError(f"{result.get('error')}: {result.get('error_description')}")
    return result["access_token"]


# Hard ceiling on how many messages a single fetch will page through, even when
# `count=0` (unbounded) — protects against a pathologically large mailbox turning
# one sync into an unbounded number of Graph API calls. 2,000 comfortably covers
# a "full sync" backlog catch-up in a handful of pages (Graph pages at up to 999).
_MAX_SCANNED = 2000


def fetch_latest(
    *, tenant_id: str, client_id: str, client_secret: str, mailbox: str, count: int,
    known_uids: set[str] | None = None, since_at: datetime | None = None,
) -> FetchResult:
    """Returns up to `count` not-yet-synced messages, **oldest first**. `count=0`
    means unbounded — fetch everything in this batch (the caller loops for
    anything beyond that).

    Oldest-first is deliberate, not incidental: the caller advances its sync
    watermark forward by the latest `receivedDateTime` examined each batch.
    Newest-first would jump that watermark straight to "now" after a single
    page, making a bigger backlog look "caught up" immediately — this walks
    forward through history instead, so a mailbox with tens of thousands of
    older messages actually gets to all of them (across as many batches/runs
    as it takes), not just the newest page.

    `since_at`, when set, narrows the query itself to `receivedDateTime gt
    {since_at}` instead of scanning the whole mailbox — the scope optimization
    that makes a normal sync cheap regardless of how many thousands of older
    messages exist. Leave it unset for a first-ever sync or a forced full rescan.

    `known_uids` (already-synced UIDs for this org) is filtered out page by page
    while paginating — belt-and-suspenders dedup independent of the watermark
    above, so staleness there can only cause redundant work, never a duplicate."""
    token = _get_token(tenant_id, client_id, client_secret)
    headers = {"Authorization": f"Bearer {token}"}
    page_size = min(count, 999) if count else 999

    out: list[dict] = []
    scanned = 0
    max_received_at: datetime | None = None
    url: str | None = f"{GRAPH_BASE}/users/{mailbox}/mailFolders/inbox/messages"
    params: dict | None = {
        "$top": page_size,
        "$orderby": "receivedDateTime asc",
        "$select": "id,subject,from,receivedDateTime,bodyPreview,body",
    }
    if since_at:
        iso = since_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        params["$filter"] = f"receivedDateTime gt {iso}"
    while url and scanned < _MAX_SCANNED and (not count or len(out) < count):
        resp = requests.get(url, headers=headers, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        for m in data.get("value", []):
            scanned += 1
            received = m.get("receivedDateTime")
            received_at = datetime.fromisoformat(received.replace("Z", "+00:00")) if received else None
            if received_at and (max_received_at is None or received_at > max_received_at):
                max_received_at = received_at
            if known_uids and m["id"] in known_uids:
                continue
            sender = (m.get("from") or {}).get("emailAddress", {}) or {}
            body_field = m.get("body") or {}
            # Graph returns HTML by default unless the caller negotiates otherwise —
            # pass its contentType through so the pipeline knows to sanitize/render
            # it as HTML.
            content_type = "html" if (body_field.get("contentType") or "").lower() == "html" else "text"
            out.append({
                "uid": m["id"],
                "from": sender.get("name") or sender.get("address", ""),
                "fromEmail": sender.get("address", ""),
                "subject": m.get("subject", ""),
                "body": body_field.get("content") or m.get("bodyPreview", ""),
                "contentType": content_type,
                "receivedAt": received_at,
            })
            if count and len(out) >= count:
                break
        url = data.get("@odata.nextLink")
        params = None  # the nextLink already encodes all query params
    return FetchResult(out, max_received_at)


def test_connection(*, tenant_id: str, client_id: str, client_secret: str, mailbox: str) -> None:
    token = _get_token(tenant_id, client_id, client_secret)
    resp = requests.get(
        f"{GRAPH_BASE}/users/{mailbox}/mailFolders/inbox",
        headers={"Authorization": f"Bearer {token}"},
        params={"$select": "id"},
        timeout=20,
    )
    resp.raise_for_status()


def send_reply(*, tenant_id: str, client_id: str, client_secret: str, mailbox: str,
                to_addr: str, subject: str, body: str) -> None:
    """Sends via POST /users/{mailbox}/sendMail — requires Mail.Send (application
    permission, admin-consented) on the same app registration used for reading.
    saveToSentItems=true so, unlike raw SMTP, this also lands a copy in Sent."""
    token = _get_token(tenant_id, client_id, client_secret)
    resp = requests.post(
        f"{GRAPH_BASE}/users/{mailbox}/sendMail",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to_addr}}],
            },
            "saveToSentItems": "true",
        },
        timeout=20,
    )
    resp.raise_for_status()
