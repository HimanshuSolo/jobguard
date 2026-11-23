"""PII redaction before any hosted LLM call (Phase 3).

Emails, phone numbers, and long digit runs (account/card/Aadhaar-shaped) are
replaced with placeholders. Names are NOT redacted here (too noisy; the
pipeline drops raw bodies and only forwards structured fields). The report
lists placeholder counts only — never the redacted values.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Phone candidates: digit runs with optional +/separators; filtered below so
# short bare numbers (7 digits, no +, no separators) survive untouched.
_CANDIDATE_RE = re.compile(r"(?<!\d)\+?\d[\d\s\-.]*\d(?!\d)")
_LONG_DIGITS_RE = re.compile(r"(?<!\d)\d{12,19}(?!\d)")


def _is_phone(candidate: str) -> bool:
    digits = re.sub(r"\D", "", candidate)
    if len(digits) < 7:
        return False
    has_marker = candidate.strip().startswith("+") or bool(re.search(r"[\s\-.]", candidate))
    return len(digits) >= 10 or has_marker


class PiiReport(BaseModel):
    """Redacted text plus counts per placeholder type."""

    text: str = ""
    emails: int = 0
    phones: int = 0
    long_digits: int = 0


def redact_pii(text: str) -> PiiReport:
    """Replace PII with ``[REDACTED_*]`` placeholders."""
    emails = len(_EMAIL_RE.findall(text))
    redacted = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    long_digits = len(_LONG_DIGITS_RE.findall(redacted))
    redacted = _LONG_DIGITS_RE.sub("[REDACTED_ID]", redacted)
    phones = 0

    def _scrub(match: re.Match[str]) -> str:
        nonlocal phones
        if _is_phone(match.group(0)):
            phones += 1
            return "[REDACTED_PHONE]"
        return match.group(0)

    redacted = _CANDIDATE_RE.sub(_scrub, redacted)
    return PiiReport(text=redacted, emails=emails, phones=phones, long_digits=long_digits)
