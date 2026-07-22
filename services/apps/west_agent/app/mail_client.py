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


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _extract_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition") or "")
            if part.get_content_type() == "text/plain" and "attachment" not in disp:
                raw = part.get_payload(decode=True) or b""
                return raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                raw = part.get_payload(decode=True) or b""
                return raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    raw = msg.get_payload(decode=True)
    if raw:
        return raw.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return msg.get_payload() or ""


def fetch_latest(*, imap_host: str, imap_port: int, username: str, password: str, count: int) -> list[dict]:
    conn = imaplib.IMAP4_SSL(imap_host, imap_port)
    try:
        conn.login(username, password)
        conn.select("INBOX")
        _, data = conn.uid("search", None, "ALL")
        uids = data[0].split()
        if count:
            uids = uids[-count:]
        out: list[dict] = []
        for uid in reversed(uids):
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
            out.append({
                "uid": uid.decode(),
                "from": _decode(name) or addr,
                "fromEmail": addr,
                "subject": _decode(msg.get("Subject", "")),
                "body": _extract_body(msg),
                "receivedAt": received,
            })
        return out
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
