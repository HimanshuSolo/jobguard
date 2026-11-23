"""Prompt-injection heuristics (Phase 3).

Deterministic pattern scan over untrusted text. It does NOT decide safety on
its own: the result feeds the Phase 4 pipeline, which quarantines or drops
flagged items (plus an LLM judge). Patterns target direct instructions,
role-reassignment, exfiltration requests, and urgency-based manipulation.
Benign career-page prose ("follow the application instructions") must NOT
match — covered by false-refusal eval cases.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        # The instruction object must be qualified (previous/prior/your/...):
        # bare "application instructions" in career prose must NOT match.
        "ignore-instructions",
        re.compile(
            r"(?i)\b(ignore|disregard|forget|override)\b.{0,40}\b("
            r"previous|prior|above|earlier|your|these|all|original|system"
            r")(\s+\w+){0,2}\s+(instructions?|prompts?|rules?)\b"
            r"|\b(system prompt)\b"
        ),
    ),
    (
        # "new role" alone matches innocent prose ("new role descriptions");
        # require an agentive anchor (you/your/act/assume/adopt) nearby.
        "role-reassign",
        re.compile(
            r"(?i)\b(you are (now|a)|act as|pretend (to be|you are)|system prompt)\b"
            r"|\bnew role\b.{0,30}\b(you|your|act|assume|adopt)\b"
            r"|\b(you|your)\b.{0,30}\bnew role\b"
        ),
    ),
    (
        "exfiltrate",
        re.compile(
            r"(?i)\b(send|email|forward|post|upload|reveal|disclose|paste)\b.{0,50}\b("
            r"password|token|secret|api[-_ ]?key|credentials|resume|ssn|bank)\b"
        ),
    ),
    (
        "tool-coerce",
        re.compile(
            r"(?i)\b(approve (this|the)|send (an? )?email to|transfer|wire .{0,20}(money|funds)|"
            r"click here to (claim|verify|confirm))\b"
        ),
    ),
    (
        "jailbreak-phrase",
        re.compile(r"(?i)\b(do anything now|dan mode|jailbreak|developer mode|no restrictions)\b"),
    ),
    (
        "hidden-imperative",
        re.compile(r"(?i)\b(do not (tell|mention|reveal|show).{0,40}(user|owner|human))\b"),
    ),
]


class InjectionFinding(BaseModel):
    """One matched injection rule with the offending snippet (truncated)."""

    rule: str
    snippet: str


class InjectionReport(BaseModel):
    """Scan verdict: flagged items are quarantined downstream, never auto-dropped silently."""

    flagged: bool = False
    findings: list[InjectionFinding] = Field(default_factory=list)


def scan_injection(text: str) -> InjectionReport:
    """Run all injection rules; benign text yields ``flagged=False``."""
    findings: list[InjectionFinding] = []
    for rule, pattern in _RULES:
        match = pattern.search(text)
        if match:
            start = max(0, match.start() - 20)
            findings.append(
                InjectionFinding(rule=rule, snippet=text[start : match.end() + 20][:160])
            )
    return InjectionReport(flagged=bool(findings), findings=findings)
