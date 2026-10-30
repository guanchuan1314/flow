"""Outbound email. Sent on a background thread so requests never wait on SMTP.

With no SMTP_HOST configured, only the recipient and subject are logged (dev mode). Bodies are never
logged: they hold password reset links and personal data. `outbox` keeps the last messages for tests.
"""

import logging
import smtplib
import ssl
import threading
from email.message import EmailMessage

from app.config import get_settings
from app.services import branding

log = logging.getLogger(__name__)

# Tests and tooling can inspect what would have been sent.
outbox: list[EmailMessage] = []


def _deliver(msg: EmailMessage) -> None:
    s = get_settings()
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
            if s.smtp_starttls:
                smtp.starttls(context=ssl.create_default_context())
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
        log.info("email sent to %s: %s", msg["To"], msg["Subject"])
    except Exception:
        log.exception("failed to send email to %s: %s", msg["To"], msg["Subject"])


def send_email(to: str, subject: str, body: str) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s.smtp_from, to, f"{branding.current().app_name}: {subject}"
    msg.set_content(body)
    outbox.append(msg)
    del outbox[:-100]
    if not s.smtp_host:
        log.info("email not sent (SMTP not configured) to=%s subject=%s", to, subject)
        return
    threading.Thread(target=_deliver, args=(msg,), daemon=True, name="email").start()


def notify_client(client, subject: str, body: str) -> None:
    send_email(client.email, subject, f"Hello {client.first_name},\n\n{body}\n\n{get_settings().public_url}/portal")
