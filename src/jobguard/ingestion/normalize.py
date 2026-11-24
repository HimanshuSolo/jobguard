"""Normalization: SourceItem -> NormalizedPosting + canonical_key dedupe key.

``canonical_key = sha256(normalized company | role | location | url)`` gives
cross-platform dedupe: the same job mailed by LinkedIn and listed on a board
collapses to one posting; ``seen_on`` records every source that carried it.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import BaseModel, Field

from jobguard.sources.base import SourceItem

_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
}
_WS_RE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    """Collapse whitespace and casefold for key building."""
    return _WS_RE.sub(" ", value.strip()).casefold()


def normalize_url(url: str) -> str:
    """Canonical URL: lowercase scheme/host, drop tracking params + fragment."""
    url = url.strip()
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else "https://" + url)
    query = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() not in _TRACKING_PARAMS]
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            urlencode(query),
            "",
        )
    )


def parse_when(value: str) -> datetime | None:
    """ISO-8601-ish timestamp -> naive UTC datetime (None when unparseable)."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def canonical_key(company: str, role: str, location: str, link: str) -> str:
    """Stable cross-platform identity hash for one posting."""
    joined = "|".join(
        [
            normalize_text(company),
            normalize_text(role),
            normalize_text(location),
            normalize_url(link),
        ]
    )
    return hashlib.sha256(joined.encode()).hexdigest()


class NormalizedPosting(BaseModel):
    """Adapter-neutral posting, pre-dedupe (``seen_on`` merged by dedupe)."""

    company: str = ""
    role: str = ""
    location: str = ""
    link: str = ""
    skills: list[str] = Field(default_factory=list)
    eligibility: str = ""
    source: str = ""
    sender: str = ""
    body: str = ""
    seen_on: list[str] = Field(default_factory=list)
    published: str = ""


def _mail_published(item: SourceItem, parsed: dict[str, Any]) -> str:
    """Email date, minus the "posted N days ago" age a digest card shows."""
    received = parse_when(str(item.payload.get("received_at", "")))
    if received is None:
        return ""
    age = parsed.get("age_days")
    if isinstance(age, int) and 0 <= age <= 120:
        received -= timedelta(days=age)
    return received.isoformat()


def _from_mail(item: SourceItem) -> NormalizedPosting:
    parsed = item.payload.get("parsed") or {}
    if not isinstance(parsed, dict):
        parsed = {}
    return NormalizedPosting(
        company=str(parsed.get("company", "")),
        role=str(parsed.get("role", "")),
        location=str(parsed.get("location", "")),
        link=str(parsed.get("link", "")),
        source=item.source,
        sender=str(item.payload.get("sender", "")),
        body=str(item.payload.get("body_text", ""))[:4000],
        seen_on=[item.source],
        published=_mail_published(item, parsed),
    )


def _from_board(item: SourceItem) -> NormalizedPosting:
    payload = item.payload
    description = str(payload.get("description_text", ""))
    return NormalizedPosting(
        company=str(payload.get("company", "")),
        role=str(payload.get("title", "")),
        location=str(payload.get("location", "")),
        link=str(payload.get("url", "")),
        eligibility=description[:2000],
        source=item.source,
        body=description[:4000],
        seen_on=[item.source],
        published=str(payload.get("published", "")),
    )


def _from_rss(item: SourceItem) -> NormalizedPosting:
    payload = item.payload
    summary = str(payload.get("summary", ""))
    return NormalizedPosting(
        company="",
        role=str(payload.get("title", "")),
        link=str(payload.get("link", "")),
        eligibility=summary[:2000],
        source=item.source,
        body=summary[:4000],
        seen_on=[item.source],
        published=str(payload.get("published", "")),
    )


def normalize_item(item: SourceItem) -> NormalizedPosting:
    """Map any adapter item to the neutral posting shape."""
    if item.source in ("gmail", "imap"):
        return _from_mail(item)
    if item.source.startswith("board:"):
        return _from_board(item)
    if item.source.startswith("rss:"):
        return _from_rss(item)
    payload: dict[str, Any] = item.payload
    return NormalizedPosting(
        company=str(payload.get("company", "")),
        role=str(payload.get("title", payload.get("role", ""))),
        location=str(payload.get("location", "")),
        link=str(payload.get("link", payload.get("url", ""))),
        source=item.source,
        seen_on=[item.source],
    )


def key_of(post: NormalizedPosting) -> str:
    """Canonical key for an already-normalized posting."""
    return canonical_key(post.company, post.role, post.location, post.link)
