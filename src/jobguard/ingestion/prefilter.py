"""Sender / role / location prefilter: drop obvious non-matches before any LLM call.

Runs on normalized postings (free) so the per-run LLM budget is spent only on
plausible developer / SDE-intern roles from known alert senders. Mail from an
unrecognized sender (e.g. a company career address) passes the sender gate on
benefit of doubt; the role and location gates still apply.

The role gate matches whole words in the *title* (and extracted skills) only.
It used to substring-match title + company + location + description, so
"ml" hit "html", "data" hit every description, and nearly everything passed.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from jobguard.ingestion.normalize import NormalizedPosting
from jobguard.relevance import (
    batch_ok,
    is_dev_role,
    is_intern_role,
    is_job_alert_mail,
    location_ok,
)
from jobguard.sources.parsers import detect_platform

DEFAULT_KEYWORDS = (
    "sde,sdet,software,developer,engineer,engineering,programmer,backend,"
    "frontend,full stack,full-stack,fullstack,devops,sre,web,mobile,android,"
    "ios,flutter,react,node,python,java,golang,rust,ml,ai,machine learning,"
    "data,cloud,qa,test automation"
)
DEFAULT_SENDERS = (
    "linkedin,indeed,naukri,internshala,unstop,glassdoor,hirist,instahyre,"
    "wellfound,cutshort,foundit,apna"
)


class Prefilter(BaseModel):
    """Gate rules; empty lists mean 'no filtering on that axis'."""

    keywords: list[str] = Field(default_factory=list)
    senders: list[str] = Field(default_factory=list)
    india_only: bool = False
    extra_locations: list[str] = Field(default_factory=list)
    intern_only: bool = False
    grad_year: int = 0

    @classmethod
    def from_settings(
        cls,
        keywords_csv: str,
        senders_csv: str,
        *,
        location_scope: str = "any",
        extra_locations: list[str] | None = None,
        role_level: str = "any",
        grad_year: int = 0,
    ) -> Prefilter:
        """Build from comma-separated settings strings."""
        keywords = [k.strip().casefold() for k in keywords_csv.split(",") if k.strip()]
        senders = [s.strip().casefold() for s in senders_csv.split(",") if s.strip()]
        return cls(
            keywords=keywords,
            senders=senders,
            india_only=location_scope.strip().casefold() == "india",
            extra_locations=list(extra_locations or []),
            intern_only=role_level.strip().casefold() == "intern",
            grad_year=grad_year,
        )


def keep(post: NormalizedPosting, sender: str, prefilter: Prefilter) -> tuple[bool, str]:
    """Decide whether ``post`` survives; returns (keep, reason)."""
    if prefilter.senders and post.source in ("gmail", "imap"):
        if detect_platform(sender) != "unknown" and not any(
            s in sender.casefold() for s in prefilter.senders
        ):
            return False, f"sender {sender!r} not in prefilter senders"
    if post.source in ("gmail", "imap"):
        # Single-job mails carry the subject as role: catches stored replies.
        alert, reason = is_job_alert_mail(post.role, sender)
        if not alert:
            return False, reason
    if prefilter.keywords and not is_dev_role(post.role, post.skills, prefilter.keywords):
        return False, "no prefilter keyword matched"
    if prefilter.intern_only and not is_intern_role(post.role):
        return False, "not an internship title"
    if prefilter.grad_year:
        text = " ".join([post.role, post.eligibility, post.body])
        ok, reason = batch_ok(text, prefilter.grad_year)
        if not ok:
            return False, reason
    if prefilter.india_only:
        ok, reason = location_ok(post.location, prefilter.extra_locations)
        if not ok:
            return False, reason
    return True, "matched"
