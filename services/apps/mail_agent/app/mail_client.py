"""IMAP fetch + SMTP send (stdlib). Sync functions — call via run_in_threadpool."""

from __future__ import annotations

import email
import imaplib
import smtplib
import ssl
from datetime import timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from typing import NamedTuple


class FetchResult(NamedTuple):
    messages: list[dict]
    # The highest UID this batch's IMAP SEARCH examined (whether or not every
    # message in it was individually fetched successfully) — the caller advances
    # its sync watermark to this, so a batch that hits its `count` cap partway
    # through a bigger backlog resumes from the right place next time, and a
    # transient single-message fetch failure doesn't get silently re-attempted
    # forever either.
    max_uid_seen: str | None


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _extract_body(msg: email.message.Message) -> tuple[str, str]:
    """Returns (body, content_type) where content_type is "text" or "html"."""
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition") or "")
            if part.get_content_type() == "text/plain" and "attachment" not in disp:
                raw = part.get_payload(decode=True) or b""
                return raw.decode(part.get_content_charset() or "utf-8", errors="replace"), "text"
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                raw = part.get_payload(decode=True) or b""
                return raw.decode(part.get_content_charset() or "utf-8", errors="replace"), "html"
        return "", "text"
    content_type = "html" if msg.get_content_type() == "text/html" else "text"
    raw = msg.get_payload(decode=True)
    if raw:
        return raw.decode(msg.get_content_charset() or "utf-8", errors="replace"), content_type
    return msg.get_payload() or "", content_type


def _extract_attachments(msg: email.message.Message) -> list[dict]:
    """Forwarded to the CRM alongside a referral/communication (crm_sync.py) — never
    persisted to our own DB, same "don't store the binary" precedent as call audio."""
    if not msg.is_multipart():
        return []
    out: list[dict] = []
    for part in msg.walk():
        disp = str(part.get("Content-Disposition") or "")
        filename = part.get_filename()
        if not filename or ("attachment" not in disp and "inline" not in disp):
            continue
        data = part.get_payload(decode=True)
        if not data:
            continue
        out.append({
            "filename": _decode(filename),
            "content_type": part.get_content_type() or "application/octet-stream",
            "data": data,
        })
    return out


def fetch_latest(
    *, imap_host: str, imap_port: int, username: str, password: str, count: int,
    known_uids: set[str] | None = None, since_uid: str | None = None,
) -> FetchResult:
    """Returns up to `count` not-yet-synced messages, **oldest first**. `count=0`
    means unbounded — fetch everything in this batch (the caller loops for
    anything beyond that).

    Oldest-first is deliberate, not incidental: the caller advances its sync
    watermark forward by the highest UID *examined* each batch. Taking the
    newest UIDs first would jump that watermark straight to the mailbox's
    ceiling after a single batch, making a bigger backlog look "caught up"
    immediately — this walks it forward through history instead, so a mailbox
    with tens of thousands of older messages actually gets to all of them
    (across as many batches/runs as it takes), not just the newest slice.

    `since_uid`, when set, narrows the IMAP SEARCH itself to `UID {since_uid+1}:*`
    instead of scanning the whole mailbox (`ALL`) — the scope optimization that
    makes a normal sync cheap regardless of how many thousands of older messages
    exist. Leave it unset for a first-ever sync (nothing to narrow by yet) or a
    forced full rescan.

    `known_uids` (already-synced UIDs for this org) is filtered out *before* the
    `count` slice, not after — belt-and-suspenders dedup independent of the
    watermark above, so a stale/incorrect `since_uid` can never cause a
    duplicate insert, only redundant work."""
    conn = imaplib.IMAP4_SSL(imap_host, imap_port)
    try:
        conn.login(username, password)
        conn.select("INBOX")
        criteria = f"UID {int(since_uid) + 1}:*" if since_uid else "ALL"
        _, data = conn.uid("search", None, criteria)
        uids = data[0].split()  # ascending order per IMAP convention
        if known_uids:
            uids = [u for u in uids if u.decode() not in known_uids]
        if count:
            uids = uids[:count]
        max_uid_seen = max((u.decode() for u in uids), key=int, default=None)
        out: list[dict] = []
        for uid in uids:
            _, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            name, addr = parseaddr(msg.get("From", ""))
            try:
                received = parsedate_to_datetime(msg.get("Date"))
                if received and received.tzinfo is None:
                    received = received.replace(tzinfo=timezone.utc)
            except Exception:
                received = None
            body, content_type = _extract_body(msg)
            out.append({
                "uid": uid.decode(),
                "from": _decode(name) or addr,
                "fromEmail": addr,
                "subject": _decode(msg.get("Subject", "")),
                "body": body,
                "contentType": content_type,
                "receivedAt": received,
                "attachments": _extract_attachments(msg),
            })
        return FetchResult(out, max_uid_seen)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def test_connection(*, imap_host: str, imap_port: int, username: str, password: str) -> None:
    conn = imaplib.IMAP4_SSL(imap_host, imap_port)
    try:
        conn.login(username, password)
        conn.select("INBOX")
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def send_reply(*, smtp_host: str, smtp_port: int, username: str, password: str,
               to_addr: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = username
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            server.login(username, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(username, password)
            server.send_message(msg)
