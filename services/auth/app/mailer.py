"""Tiny SMTP mailer for teammate invites. Blocking (call via run_in_threadpool).

Config comes from the Auth service .env (SMTP_*). When SMTP_HOST is unset the send is
a graceful no-op so the invite flow still works (the inviter shares the link manually).
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .config import settings

log = logging.getLogger("auth.mailer")


def send_invite_email(*, to: str, org_name: str, role: str, accept_link: str) -> bool:
    """Send an invite email. Returns True if sent, False if SMTP isn't configured.
    Raises on an actual SMTP failure so the caller can surface it."""
    if not settings.smtp_configured:
        log.info("SMTP not configured — invite email to %s skipped", to)
        return False

    msg = EmailMessage()
    msg["Subject"] = f"You've been invited to {org_name}"
    msg["From"] = settings.smtp_from_header
    msg["To"] = to
    msg.set_content(
        f"You've been invited to join {org_name} as {role}.\n\n"
        f"Set your password and get started:\n{accept_link}\n\n"
        "This link is one-time and expires in 7 days.\n"
    )

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_user and settings.smtp_password:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
    log.info("invite email sent to %s", to)
    return True


def send_password_reset_email(*, to: str, reset_link: str) -> bool:
    """Send a password-reset email. Returns True if sent, False if SMTP isn't configured.
    Raises on an actual SMTP failure so the caller can surface it."""
    if not settings.smtp_configured:
        log.info("SMTP not configured — reset email to %s skipped", to)
        return False

    msg = EmailMessage()
    msg["Subject"] = "Reset your password"
    msg["From"] = settings.smtp_from_header
    msg["To"] = to
    msg.set_content(
        "We received a request to reset your password.\n\n"
        f"Choose a new password:\n{reset_link}\n\n"
        "This link is one-time and expires in 2 hours. If you didn't request this, "
        "you can ignore this email.\n"
    )

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_user and settings.smtp_password:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
    log.info("password reset email sent to %s", to)
    return True
