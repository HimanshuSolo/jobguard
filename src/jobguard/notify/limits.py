"""Send policy: recipient lock + daily cap (Phase 5).

The LLM can never set recipients: `check_sendable` requires `to` to equal
`owner_email` or an address in the configured `DIGEST_RECIPIENTS` allowlist
(environment only, never content), and refuses when today's sent-mail count reaches the cap. The
cap counts `digests_sent` rows (every send path records one), so digests and
urgent mails share one budget.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from jobguard.store.models import DigestSent


def check_sendable(
    session: Session,
    *,
    to: str,
    owner_email: str,
    max_per_day: int,
    extra_recipients: Collection[str] = (),
) -> tuple[bool, str]:
    """Return (allowed, reason) for one send request."""
    allowed = {e.strip().casefold() for e in [owner_email, *extra_recipients] if e.strip()}
    if not owner_email or to.strip().casefold() not in allowed:
        return False, "recipient must be OWNER_EMAIL or listed in DIGEST_RECIPIENTS"
    today = datetime.now(UTC).date()
    sent_today = sum(1 for row in session.query(DigestSent).all() if row.sent_at.date() == today)
    if sent_today >= max_per_day:
        return False, f"daily cap reached ({sent_today}/{max_per_day})"
    return True, "allowed"
