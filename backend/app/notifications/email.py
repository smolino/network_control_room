"""Outbound email for the Human Review "send to SOC/maintenance" action.

There's no mail server in docker-compose.yml, so this only actually sends
when SMTP_HOST is set in the environment (see app/config.py). Without it,
send_email logs the message and reports "simulated" rather than failing -
the same pattern the remediation engine uses for its synthetic
backups/actions, so the audit trail (who this was sent to, what it said)
is real even though delivery isn't.
"""

import logging
import smtplib
from email.message import EmailMessage

from app.config import settings
from app.models import NotificationStatus

logger = logging.getLogger(__name__)


def send_email(to: str, subject: str, body: str) -> tuple[NotificationStatus, str | None]:
    """Returns (status, error). error is only set when status is FAILED."""
    if not settings.smtp_host:
        logger.info("SMTP not configured - simulating email to %s: %s", to, subject)
        return NotificationStatus.SIMULATED, None

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg.set_content(body)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
        return NotificationStatus.SENT, None
    except Exception as exc:  # noqa: BLE001 - report any SMTP failure, don't crash the request
        logger.exception("Failed to send email to %s", to)
        return NotificationStatus.FAILED, str(exc)
