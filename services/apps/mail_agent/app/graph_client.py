"""Microsoft Graph mail fetch + send (app-only / client-credentials). Sync — call via run_in_threadpool.

Alternative to mail_client's IMAP fetch and SMTP send for orgs whose tenant has
IMAP Basic Auth disabled but has an Entra app registration with Mail.Read and
Mail.Send (application permissions, admin-consented). Selecting Graph as the
read provider covers both directions — there is no separate SMTP step.
"""

from __future__ import annotations

import base64
import logging
import time
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

log = logging.getLogger("mail_agent.graph")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPE = ["https://graph.microsoft.com/.default"]

#: Fields fetched per message. The recipient fields and internetMessageHeaders carry
#: the receiving-address evidence; conversationId and internetMessageId are thread
#: matching and the second dedup signal respectively.
#:
#: internetMessageId is deliberately NOT a uniqueness key anywhere: the same message
#: legitimately lands in two monitored mailboxes carrying an identical value, and
#: treating it as an identity would silently drop the second copy.
_SELECT_FIELDS = (
    "id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
    "bodyPreview,body,hasAttachments,conversationId,internetMessageId,"
    "internetMessageHeaders"
)


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


def _fetch_attachments(token: str, mailbox: str, message_id: str) -> list[dict]:
    """Forwarded to the CRM alongside a referral/communication (crm_sync.py) — never
    persisted to our own DB, same "don't store the binary" precedent as call audio."""
    try:
        resp = requests.get(
            f"{GRAPH_BASE}/users/{mailbox}/messages/{message_id}/attachments",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_ACTION_TIMEOUT,
        )
        resp.raise_for_status()
    except Exception:
        return []
    out: list[dict] = []
    for a in resp.json().get("value", []):
        if a.get("@odata.type") != "#microsoft.graph.fileAttachment" or not a.get("contentBytes"):
            continue
        out.append({
            "filename": a.get("name") or "attachment",
            "content_type": a.get("contentType") or "application/octet-stream",
            "data": base64.b64decode(a["contentBytes"]),
        })
    return out


# Hard ceiling on how many messages a single fetch will page through, even when
# `count=0` (unbounded) — protects against a pathologically large mailbox turning
# one sync into an unbounded number of Graph API calls. 2,000 comfortably covers
# a "full sync" backlog catch-up in a handful of pages (Graph pages at up to 999).
_MAX_SCANNED = 2000

# Graph can be slow to serve the first page of a very large mailbox, especially
# ordered newest-first: info@ holds 85,120 inbox items and a 20s read timeout was
# not enough to seed it. The cost of waiting is a slower sync; the cost of timing
# out is a mailbox that silently never seeds, so this errs long.
_LIST_TIMEOUT = 90
_ACTION_TIMEOUT = 30

# Graph answers a request it cannot serve right now with 429 (throttled) or
# 503/504 (busy), and both are transient. Seeding info@ -- 85,120 inbox items,
# ordered newest-first, with internetMessageHeaders -- reliably provokes one.
# Without a retry the mailbox simply never seeds, and the only symptom is an
# absence, so this is treated as normal operation rather than an error.
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 4


def _get_with_retry(url: str, *, headers: dict, params: dict | None,
                    timeout: int) -> requests.Response:
    """GET, honouring Retry-After and backing off on transient Graph failures."""
    delay = 2.0
    last: requests.Response | None = None
    for attempt in range(_MAX_RETRIES + 1):
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        if resp.status_code not in _RETRY_STATUS:
            return resp
        last = resp
        if attempt == _MAX_RETRIES:
            break
        # Graph's own Retry-After is authoritative when present; backing off
        # faster than it asks is how a throttle becomes a longer throttle.
        wait = delay
        hdr = resp.headers.get("Retry-After")
        if hdr:
            try:
                wait = max(wait, float(hdr))
            except ValueError:
                pass
        log.info("graph %s on %s - retrying in %.0fs (attempt %d/%d)",
                 resp.status_code, url.rsplit("/", 2)[-2:], wait,
                 attempt + 1, _MAX_RETRIES)
        time.sleep(min(wait, 60))
        delay *= 2
    return last  # type: ignore[return-value]


def fetch_latest(
    *, tenant_id: str, client_id: str, client_secret: str, mailbox: str, count: int,
    known_uids: set[str] | None = None, since_at: datetime | None = None,
    newest_first: bool = False,
) -> FetchResult:
    """Returns up to `count` not-yet-synced messages. `count=0` means unbounded —
    fetch everything in this batch (the caller loops for anything beyond that).

    Two orderings, for two different jobs:

    `newest_first=True` — the FIRST sync of a mailbox. Takes the most recent
    `count` messages and stops. Project West's rule is "seed each mailbox with its
    latest 10 and never reach further back", so the first sync must start at the
    top: the 10 *oldest* messages in info@ are from 2011 and are of no use to
    anyone. The watermark then sits at "now" by design, because there is
    deliberately no backfill.

    `newest_first=False` (default) — every subsequent sync, walking forward from
    the watermark. Oldest-first here is deliberate: the caller advances its
    watermark by the latest `receivedDateTime` examined each batch, so taking the
    newest first would skip everything in between and silently lose mail that
    arrived while a large batch was being processed.

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
        "$orderby": "receivedDateTime desc" if newest_first else "receivedDateTime asc",
        # toRecipients/ccRecipients and internetMessageHeaders are what make the
        # receiving address recoverable — see receiving_address.py. They are fields
        # on a message the app already reads, so this needs no new Graph permission.
        # Capturing them is not optional: the business entry point exists only in
        # these headers at the moment of collection, and cannot be reconstructed from
        # a stored row afterwards.
        "$select": _SELECT_FIELDS,
    }
    if since_at:
        iso = since_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        params["$filter"] = f"receivedDateTime gt {iso}"
    while url and scanned < _MAX_SCANNED and (not count or len(out) < count):
        resp = _get_with_retry(url, headers=headers, params=params, timeout=_LIST_TIMEOUT)
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
                "attachments": _fetch_attachments(token, mailbox, m["id"]) if m.get("hasAttachments") else [],
                # Receiving-address evidence, passed through raw. The pipeline hands
                # these to receiving_address.resolve() and persists them verbatim as
                # recipient_evidence, so a determination can be audited or re-derived
                # later without the original message.
                "toRecipients": m.get("toRecipients") or [],
                "ccRecipients": m.get("ccRecipients") or [],
                "internetMessageHeaders": m.get("internetMessageHeaders") or [],
                "conversationId": m.get("conversationId"),
                "internetMessageId": m.get("internetMessageId"),
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
        timeout=_ACTION_TIMEOUT,
    )
    resp.raise_for_status()


def build_send_payload(*, to_addr: str, subject: str, body: str,
                       from_address: str | None = None) -> dict:
    """The sendMail request body. Split out so it can be asserted in tests without
    a tenant — notably that `from` is present and is never caller-chosen."""
    message: dict = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": to_addr}}],
    }
    if from_address:
        # The business identity the sender originally used. Omitted entirely (rather
        # than set to the mailbox address) when the caller derived no alias, so Graph
        # falls back to the mailbox's own address and we send one fewer assertion.
        message["from"] = {"emailAddress": {"address": from_address}}
    return {"message": message, "saveToSentItems": "true"}


def send_reply(*, tenant_id: str, client_id: str, client_secret: str, mailbox: str,
                to_addr: str, subject: str, body: str,
                from_address: str | None = None) -> None:
    """Sends via POST /users/{mailbox}/sendMail.

    `mailbox` is the physical shared mailbox the message is sent *from*; it must be
    inside the application's Exchange RBAC resource scope. `from_address` is the
    business identity on the message — the alias, where the original arrived at one —
    and is **derived by send_identity.derive(), never supplied by a caller**.

    saveToSentItems=true so the reply lands in the correct shared mailbox's Sent
    Items, which is what the brief asks for and comes free once the right mailbox is
    addressed.

    Sending as a proxy address under an app-only token behaves differently across
    tenant configurations. If this tenant rejects it, the fallback is an explicit
    Send As grant for the service principal on each alias — a tenant change, not a
    code change.
    """
    token = _get_token(tenant_id, client_id, client_secret)
    resp = requests.post(
        f"{GRAPH_BASE}/users/{mailbox}/sendMail",
        headers={"Authorization": f"Bearer {token}"},
        json=build_send_payload(to_addr=to_addr, subject=subject, body=body,
                                from_address=from_address),
        timeout=_ACTION_TIMEOUT,
    )
    resp.raise_for_status()
