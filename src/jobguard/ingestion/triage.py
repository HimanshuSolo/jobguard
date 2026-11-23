"""Local-first triage (Phase 8, Step 2.2).

Runs every non-LLM stage for one posting — guardrails, scam screen, expiry
check, cheap deterministic ranking — with zero network calls and zero
sleeping. The worker runs triage for ALL pending items first, persists
terminal outcomes immediately, and only then spends LLM budget on survivors.
"""

from __future__ import annotations

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
