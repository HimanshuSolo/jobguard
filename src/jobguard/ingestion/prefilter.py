"""Keyword/sender prefilter: drop obvious non-matches before any LLM call.

Runs on normalized postings (free) so the per-run LLM budget is spent only on
plausible developer / SDE-intern roles from known alert senders. Mail from an
unrecognized sender (e.g. a company career address) passes the sender gate on
benefit of doubt — the keyword gate still applies.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from jobguard.ingestion.normalize import NormalizedPosting
from jobguard.sources.parsers import detect_platform

DEFAULT_KEYWORDS = (
    "sde,software,developer,engineer,intern,backend,frontend,full-stack,"
    "full stack,devops,data,ml,android,ios,qa,sdet"
)
DEFAULT_SENDERS = "linkedin,indeed,naukri,internshala,unstop,glassdoor"


class Prefilter(BaseModel):
    """Substring rules; empty lists mean 'no filtering on that axis'."""

    keywords: list[str] = Field(default_factory=list)
    senders: list[str] = Field(default_factory=list)

    @classmethod
    def from_settings(cls, keywords_csv: str, senders_csv: str) -> Prefilter:
        """Build from comma-separated settings strings."""
        keywords = [k.strip().casefold() for k in keywords_csv.split(",") if k.strip()]
        senders = [s.strip().casefold() for s in senders_csv.split(",") if s.strip()]
        return cls(keywords=keywords, senders=senders)


def keep(post: NormalizedPosting, sender: str, prefilter: Prefilter) -> tuple[bool, str]:
    """Decide whether ``post`` survives; returns (keep, reason)."""
    if prefilter.senders and post.source in ("gmail", "imap"):
        if detect_platform(sender) != "unknown" and not any(
            s in sender.casefold() for s in prefilter.senders
        ):
            return False, f"sender {sender!r} not in prefilter senders"
    if prefilter.keywords:
        haystack = " ".join(
            [post.role, post.company, post.location, post.eligibility, *post.skills]
        ).casefold()
        if not any(keyword in haystack for keyword in prefilter.keywords):
            return False, "no prefilter keyword matched"
    return True, "matched"
