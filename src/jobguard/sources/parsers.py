"""Template parsers for job-alert emails (Phase 2).

Best-effort, sender-specific extraction. These handle the known template
shapes of each platform; anything they cannot parse returns ``None`` and the
Phase 4 LLM fallback takes over. Parsers never execute or trust the content —
they only copy substrings into typed fields.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

_LABEL_PATTERNS = {
    "role": re.compile(r"(?im)^(title|role|position|job title)\s*[:\-]\s*(.+)$"),
    "company": re.compile(r"(?im)^(company|organization|employer|hiring at)\s*[:\-]\s*(.+)$"),
    "location": re.compile(r"(?im)^(location|job location|place)\s*[:\-]\s*(.+)$"),
}

URL_RE = re.compile(r"https?://[^\s<>\")]+", re.IGNORECASE)
_SUBJECT_PREFIX_RE = re.compile(
    r"(?i)^\s*(new jobs?:?|job alerts?:?|hiring:?|we'?re hiring:?|internshala:?|"
    r"naukri job alerts?:?|daily job alert:?|your job alert:?)\s*"
)
_AT_COMPANY_RE = re.compile(r"(?i)\bat\s+([A-Z][A-Za-z0-9&.,'\- ]+?)(?:\s+in\s+|\s*$)")


class AlertPosting(BaseModel):
    """Fields extracted from one alert email."""

    platform: str = "unknown"
    company: str = ""
    role: str = ""
    location: str = ""
    link: str = ""
    sender: str = ""
    subject: str = ""
    snippet: str = ""  # this job's own card text when split from a digest
    age_days: int | None = None  # "posted N days ago" shown on the card


def detect_platform(sender: str) -> str:
    """Map sender address to a known platform, else 'unknown'."""
    lowered = sender.lower()
    for needle, platform in [
        ("linkedin", "linkedin"),
        ("indeed", "indeed"),
        ("naukri", "naukri"),
        ("internshala", "internshala"),
        ("unstop", "unstop"),
        ("glassdoor", "glassdoor"),
    ]:
        if needle in lowered:
            return platform
    return "unknown"


def parse_alert(sender: str, subject: str, body_text: str) -> AlertPosting | None:
    """Extract posting fields; ``None`` when nothing job-like is found."""
    platform = detect_platform(sender)
    fields: dict[str, str] = {}
    for key, pattern in _LABEL_PATTERNS.items():
        match = pattern.search(body_text)
        if match:
            fields[key] = match.group(2).strip()
    link_match = URL_RE.search(body_text)
    link = link_match.group(0).rstrip(".,;") if link_match else ""

    role = fields.get("role", "")
    company = fields.get("company", "")
    if not role:
        cleaned = _SUBJECT_PREFIX_RE.sub("", subject).strip()
        at_match = _AT_COMPANY_RE.search(cleaned)
        if at_match:
            company = company or at_match.group(1).strip()
            role = _AT_COMPANY_RE.sub("", cleaned).strip(" -–—:|")
        elif link:
            # Subject fallback only when an apply link exists: a linkless,
            # labelless mail (personal notes, "your job alerts" headers) is
            # not a posting — return None instead of a phantom role.
            role = cleaned
    if not role and not company and not link:
        return None
    if not company and not link and len(role) <= 2:
        # Prefix-strip residue (e.g. "s" from "Your job alerts"): not a role.
        return None
    return AlertPosting(
        platform=platform,
        company=company,
        role=role,
        location=fields.get("location", ""),
        link=link,
        sender=sender,
        subject=subject,
    )
