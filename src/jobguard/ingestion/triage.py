"""Local-first triage + LLM candidate selection (Phase 8).

Triage runs every non-LLM stage for one posting (no network, no sleep).
`select_for_llm` then picks the bounded set the LLM may spend budget on:
cheap score at/above threshold, internships first, then highest cheap
score, capped at top-N. Everything else stays queued with its cheap score
recorded, explicitly "not analyzed yet".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from jobguard.agent.rank import Profile, score_posting
from jobguard.guardrails import (
    check_scam,
    collapse_boilerplate,
    redact_pii,
    scan_injection,
    strip_hidden_text,
)
from jobguard.store.models import Posting


@dataclass
class TriageResult:
    """Outcome of the local pass: terminal status or `queued` + cheap score."""

    status: str  # quarantined | flagged_scam | expired | queued
    cheap_score: float = 0.0
    reasons: list[str] = field(default_factory=list)


def triage_posting(post: Posting, *, profile: Profile, sender: str = "") -> TriageResult:
    """Triage one posting using local stages only. Never sleeps, never calls."""
    body = post.body or "\n".join([post.role or "", post.company or "", post.location or ""])
    hidden = strip_hidden_text(body)
    cleaned = collapse_boilerplate(hidden.text) or body
    if scan_injection(cleaned).flagged:
        return TriageResult(status="quarantined", reasons=["injection pattern matched"])
    redacted = redact_pii(cleaned).text
    if post.deadline is not None:
        aware = post.deadline if post.deadline.tzinfo else post.deadline.replace(tzinfo=UTC)
        if aware < datetime.now(UTC):
            return TriageResult(
                status="expired",
                reasons=[f"deadline {post.deadline.isoformat()} has passed"],
            )
    scam = check_scam(
        company=post.company or "",
        sender=sender,
        link=post.link or "",
        text=redacted,
    )
    if scam.flagged:
        return TriageResult(
            status="flagged_scam",
            reasons=[s.signal for s in scam.signals],
        )
    rank = score_posting(
        profile,
        role=post.role or "",
        location=post.location or "",
        skills=list(post.skills or []),
        text=redacted,
        deadline=post.deadline.isoformat() if post.deadline else "",
    )
    return TriageResult(status="queued", cheap_score=rank.score, reasons=list(rank.reasons))


def select_for_llm(
    posts: Sequence[Posting], *, top_n: int, min_cheap_score: float
) -> list[Posting]:
    """Bound the LLM spend: above-threshold items, internships first, then
    highest cheap score, capped at top_n. The rest stay queued."""
    eligible = [p for p in posts if (p.cheap_score or 0.0) >= min_cheap_score]
    eligible.sort(
        key=lambda p: (
            0 if "intern" in (p.role or "").casefold() else 1,
            -(p.cheap_score or 0.0),
            p.id,
        )
    )
    return eligible[: max(top_n, 0)]
