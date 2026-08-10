"""Microsoft Graph mail fetch (app-only / client-credentials). Sync — call via run_in_threadpool.

Alternative to mail_client's IMAP fetch for orgs whose tenant has IMAP Basic Auth
disabled but has an Entra app registration with Mail.Read (application permission,
admin-consented). SMTP sending is untouched and still goes through mail_client.
"""

from __future__ import annotations

from datetime import datetime

import msal
import requests

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


def fetch_latest(*, tenant_id: str, client_id: str, client_secret: str, mailbox: str, count: int) -> list[dict]:
    token = _get_token(tenant_id, client_id, client_secret)
    resp = requests.get(
        f"{GRAPH_BASE}/users/{mailbox}/mailFolders/inbox/messages",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "$top": count,
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,from,receivedDateTime,bodyPreview,body",
        },
        timeout=20,
    )
    resp.raise_for_status()
    out: list[dict] = []
    for m in resp.json().get("value", []):
        sender = (m.get("from") or {}).get("emailAddress", {}) or {}
        received = m.get("receivedDateTime")
        out.append({
            "uid": m["id"],
            "from": sender.get("name") or sender.get("address", ""),
            "fromEmail": sender.get("address", ""),
            "subject": m.get("subject", ""),
            "body": (m.get("body") or {}).get("content") or m.get("bodyPreview", ""),
            "receivedAt": datetime.fromisoformat(received.replace("Z", "+00:00")) if received else None,
        })
    return out


def test_connection(*, tenant_id: str, client_id: str, client_secret: str, mailbox: str) -> None:
    token = _get_token(tenant_id, client_id, client_secret)
    resp = requests.get(
        f"{GRAPH_BASE}/users/{mailbox}/mailFolders/inbox",
        headers={"Authorization": f"Bearer {token}"},
        params={"$select": "id"},
        timeout=20,
    )
    resp.raise_for_status()
