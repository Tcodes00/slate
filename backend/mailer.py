"""
Real email sending via SMTP when SMTP_HOST is configured, falling back to
the original dev-mode log line otherwise — same "missing optional
credential degrades gracefully, doesn't break the request" contract as
ANTHROPIC_API_KEY and REDIS_URL elsewhere in this codebase, not a special
case invented just for mail.

Verified against a real local SMTP debug server (aiosmtpd) during the
build: sent an actual message over the wire on this exact code path and
confirmed it arrived with the right envelope, subject, and body — not
just that smtplib didn't raise.

To use a real provider: set SMTP_HOST (and SMTP_USER/SMTP_PASSWORD if it
needs auth) in backend/.env. Nothing else in the codebase changes —
every caller just calls send_email().
"""

import logging
import os
import smtplib
from email.mime.text import MIMEText

logger = logging.getLogger("slate.mail")
logging.basicConfig(level=logging.INFO)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "no-reply@slate.dev")
# Real providers (SendGrid, SES, Postmark, Gmail) all expect STARTTLS on
# 587 — default on. Local debug servers like aiosmtpd's plain listener
# don't speak TLS at all, so local testing sets this to false.
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() != "false"


def send_email(to: str, subject: str, body: str) -> None:
    if not SMTP_HOST:
        logger.info(
            "\n--- DEV MODE: EMAIL NOT ACTUALLY SENT ---\n"
            "To: %s\nSubject: %s\n\n%s\n"
            "--- set SMTP_HOST (see backend/.env.example) to send this for real ---",
            to, subject, body,
        )
        return

    message = MIMEText(body)
    message["Subject"] = subject
    message["From"] = SMTP_FROM
    message["To"] = to

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            if SMTP_USE_TLS:
                server.starttls()
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to], message.as_string())
        logger.info("Email sent to %s via %s: %s", to, SMTP_HOST, subject)
    except Exception:
        # A misconfigured or unreachable mail server shouldn't break the
        # request that triggered it (signup, password reset, teammate
        # invite) — log loudly and fall through, same degrade-not-break
        # contract as everywhere else optional infrastructure is missing.
        logger.exception("Failed to send email to %s via %s — continuing without it", to, SMTP_HOST)
