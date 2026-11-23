"""Send policy: recipient lock + daily cap (Phase 5).

The LLM can never set recipients: `check_sendable` requires `to` to equal
`owner_email`, and refuses when today's sent-mail count reaches the cap. The
cap counts `digests_sent` rows (every send path records one), so digests and
urgent mails share one budget.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from jobguard.store.models import DigestSent


def check_sendable(
    session: Session,
    *,
    to: str,
    owner_email: str,
    max_per_day: int,
) -> tuple[bool, str]:
    """Return (allowed, reason) for one send request."""
    if not owner_email or to.strip().casefold() != owner_email.strip().casefold():
        return False, "recipient must equal OWNER_EMAIL"
    today = datetime.now(UTC).date()
    sent_today = sum(1 for row in session.query(DigestSent).all() if row.sent_at.date() == today)
    if sent_today >= max_per_day:
        return False, f"daily cap reached ({sent_today}/{max_per_day})"
    return True, "allowed"
