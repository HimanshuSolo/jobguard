"""Deterministic profile ranking (Phase 4).

Scores a posting against the owner's profile config WITHOUT an LLM call:
skill overlap, role-type match, internship/new-grad fit, location, and
deadline proximity. The LLM combined match+suggest call carries the primary score in the
pipeline; this rank is appended as an explained, reproducible second opinion
and orders items when the LLM is unreachable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class Profile(BaseModel):
    """Owner profile config (see data/profile.example.yaml)."""

    skills: list[str] = Field(default_factory=list)
    role_types: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    new_grad: bool = False


class RankResult(BaseModel):
    """Heuristic score (0-100) with human-readable reasons for the digest."""

    score: float = Field(ge=0.0, le=100.0)
    reasons: list[str] = Field(default_factory=list)


def load_profile(path: str) -> Profile:
    """Load a profile YAML file."""
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Profile(
        skills=[str(s) for s in raw.get("skills", [])],
        role_types=[str(s) for s in raw.get("role_types", [])],
        locations=[str(s) for s in raw.get("locations", [])],
        new_grad=bool(raw.get("new_grad", False)),
    )


def _deadline_days(deadline: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(deadline)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (parsed - datetime.now(UTC)).total_seconds() / 86400.0


def score_posting(
    profile: Profile,
    *,
    role: str,
    location: str,
    skills: list[str],
    text: str,
    deadline: str = "",
) -> RankResult:
    """Score a posting 0-100 against the profile (pure function)."""
    reasons: list[str] = []
    score = 0.0
    haystack = f"{role}\n{text}".casefold()

    matched = [
        s
        for s in profile.skills
        if s.casefold() in haystack or s.casefold() in {k.casefold() for k in skills}
    ]
    if matched:
        gained = min(40.0, 8.0 * len(matched))
        score += gained
        reasons.append(f"skill overlap ({', '.join(sorted(matched)[:5])}): +{gained:.0f}")

    role_hit = next((r for r in profile.role_types if r.casefold() in role.casefold()), "")
    if role_hit:
        score += 25.0
        reasons.append(f"role type {role_hit!r}: +25")

    if profile.new_grad and ("intern" in role.casefold() or "new grad" in haystack):
        score += 10.0
        reasons.append("internship/new-grad fit: +10")

    if any(loc.casefold() in location.casefold() for loc in profile.locations) or (
        "remote" in location.casefold()
    ):
        score += 10.0
        reasons.append("location fit: +10")

    days = _deadline_days(deadline) if deadline else None
    if days is not None and 0 <= days <= 14:
        score += 5.0
        reasons.append(f"deadline in {days:.0f}d: +5")

    return RankResult(score=min(100.0, score), reasons=reasons)
