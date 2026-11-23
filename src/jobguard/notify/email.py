"""SMTP email notifier (Phase 5).

Separate credentials from mail reading (`SENDER_SMTP_*`). Every send enforces,
in order: recipient lock + daily cap (`check_sendable`), outbound secret
scan (`scan_outbound`), then delivery. Refusals raise `SendRefused` (audited
by the caller); transport errors raise `SendFailed`. Nothing is ever sent to
an address other than the owner.
"""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any

import structlog
from sqlalchemy.orm import Session

from jobguard.notify.limits import check_sendable
from jobguard.notify.scan import scan_outbound

log = structlog.get_logger(__name__)


class SendRefused(RuntimeError):
    """Policy refusal: wrong recipient, cap exhausted, or secrets in content."""


class SendFailed(RuntimeError):
    """Transport failure after policy checks passed."""


SmtpFactory = Callable[..., Any]


class SmtpNotifier:
    """Deliver mail over SMTP. Inject `smtp_factory` in tests (no network)."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        from_address: str,
        owner_email: str,
        max_per_day: int = 20,
        smtp_factory: SmtpFactory | None = None,
    ) -> None:
        if not host or not from_address or not owner_email:
            raise ValueError("host, from_address, and owner_email are required")
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._from = from_address
        self._owner = owner_email
        self._max_per_day = max_per_day
        self._factory: SmtpFactory = smtp_factory or smtplib.SMTP

    @property
    def name(self) -> str:
        return "smtp"

    def send(
        self,
        session: Session,
        *,
        to: str,
        subject: str,
        html: str,
        text: str,
    ) -> None:
        """Policy-gated send; records nothing (caller records `DigestSent`)."""
        allowed, reason = check_sendable(
            session, to=to, owner_email=self._owner, max_per_day=self._max_per_day
        )
        if not allowed:
            raise SendRefused(reason)
        hits = scan_outbound(subject, f"{text}\n{html}")
        if hits:
            raise SendRefused(f"outbound content blocked: {', '.join(hits)}")
        message = EmailMessage()
        message["From"] = self._from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        try:
            with self._factory(self._host, self._port) as smtp:
                smtp.starttls()
                if self._user:
                    smtp.login(self._user, self._password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise SendFailed(f"SMTP delivery failed: {type(exc).__name__}") from exc
        log.info("notify.sent", to=to, subject_len=len(subject))
