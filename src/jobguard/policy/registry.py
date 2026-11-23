"""Deterministic tool-policy layer (SPEC section 7).

This is plain code, never "ask the LLM if it is safe". Every tool has a fixed
risk level; :func:`evaluate` maps (tool, context) to exactly one of
``allow`` / ``needs_approval`` / ``deny``. Risky executions create a
``PendingAction`` row (Phase 5) — this module only decides, it never executes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from pydantic import BaseModel, Field

log = structlog.get_logger(__name__)

RiskLevel = Literal["low", "medium", "high", "critical"]
Decision = Literal["allow", "needs_approval", "deny"]


@dataclass(frozen=True)
class ToolSpec:
    """Static registry entry for one tool."""

    name: str
    risk: RiskLevel
    description: str


TOOLS: dict[str, ToolSpec] = {
    "read_mail": ToolSpec(
        name="read_mail",
        risk="low",
        description="Read-only mailbox access (gmail.readonly or IMAP).",
    ),
    "fetch_feed": ToolSpec(
        name="fetch_feed",
        risk="low",
        description="Fetch RSS/board feeds from allowlisted domains.",
    ),
    "write_db": ToolSpec(
        name="write_db",
        risk="low",
        description="Write normalized postings and state to our own DB.",
    ),
    "build_digest": ToolSpec(
        name="build_digest",
        risk="low",
        description="Assemble the digest from DB rows only.",
    ),
    "fetch_job_url": ToolSpec(
        name="fetch_job_url",
        risk="medium",
        description="Fetch a single job URL: allowlist + SSRF checks + size/timeout.",
    ),
    "send_email": ToolSpec(
        name="send_email",
        risk="medium",
        description="Send email: recipient hard-locked to OWNER_EMAIL, within caps.",
    ),
    "update_master_resume": ToolSpec(
        name="update_master_resume",
        risk="high",
        description="Mutate the master resume: plan + human approval required.",
    ),
    "generate_tailored_resume": ToolSpec(
        name="generate_tailored_resume",
        risk="medium",
        description="Render a tailored resume only from resume.yaml data.",
    ),
    "submit_application": ToolSpec(
        name="submit_application",
        risk="critical",
        description="NOT IMPLEMENTED. Always denied; never automatic.",
    ),
}


class PolicyContext(BaseModel):
    """Everything ``evaluate`` may inspect. Explicit value object: no I/O."""

    tool: str = Field(description="Tool name, must exist in TOOLS.")
    recipient: str | None = Field(default=None, description="Requested email recipient, if any.")
    owner_email: str = Field(default="", description="OWNER_EMAIL from config.")
    domain_allowlisted: bool = Field(
        default=False, description="True if the target domain is on the allowlist."
    )
    ssrf_checks_passed: bool = Field(
        default=False, description="True if SSRF guards passed for the URL fetch."
    )
    within_rate_limits: bool = Field(
        default=True, description="False when rate/daily caps are exhausted."
    )


class PolicyDecision(BaseModel):
    """The single deterministic verdict for one tool invocation."""

    tool: str
    decision: Decision
    reason: str


def evaluate(ctx: PolicyContext) -> PolicyDecision:
    """Return the allow / needs_approval / deny verdict for ``ctx``."""
    spec = TOOLS.get(ctx.tool)
    if spec is None:
        verdict = PolicyDecision(
            tool=ctx.tool, decision="deny", reason="Unknown tool: not in registry."
        )
        _audit(verdict)
        return verdict

    if ctx.tool == "submit_application":
        verdict = PolicyDecision(
            tool=ctx.tool,
            decision="deny",
            reason="submit_application is not implemented and never automatic.",
        )
        _audit(verdict)
        return verdict

    if ctx.tool == "update_master_resume":
        verdict = PolicyDecision(
            tool=ctx.tool,
            decision="needs_approval",
            reason="High-risk resume mutation requires a stored plan + human approval.",
        )
        _audit(verdict)
        return verdict

    if ctx.tool == "send_email":
        if ctx.recipient is None or ctx.recipient != ctx.owner_email or not ctx.owner_email:
            verdict = PolicyDecision(
                tool=ctx.tool,
                decision="deny",
                reason="send_email recipient must equal OWNER_EMAIL; the LLM cannot set it.",
            )
            _audit(verdict)
            return verdict
        if not ctx.within_rate_limits:
            verdict = PolicyDecision(
                tool=ctx.tool,
                decision="deny",
                reason="send_email rate limit / daily cap exhausted.",
            )
            _audit(verdict)
            return verdict
        verdict = PolicyDecision(
            tool=ctx.tool,
            decision="allow",
            reason="Recipient locked to OWNER_EMAIL and within caps.",
        )
        _audit(verdict)
        return verdict

    if ctx.tool in ("fetch_job_url", "fetch_feed"):
        if not ctx.domain_allowlisted:
            verdict = PolicyDecision(
                tool=ctx.tool,
                decision="deny",
                reason="Target domain is not on the allowlist.",
            )
            _audit(verdict)
            return verdict
        if ctx.tool == "fetch_job_url" and not ctx.ssrf_checks_passed:
            verdict = PolicyDecision(
                tool=ctx.tool,
                decision="deny",
                reason="SSRF checks (private/loopback IP, size/timeout) did not pass.",
            )
            _audit(verdict)
            return verdict
        verdict = PolicyDecision(
            tool=ctx.tool,
            decision="allow",
            reason="Allowlisted domain with guards satisfied.",
        )
        _audit(verdict)
        return verdict

    # Low-risk tools (read_mail, write_db, build_digest,
    # generate_tailored_resume is medium but data-confined): allow.
    verdict = PolicyDecision(
        tool=ctx.tool, decision="allow", reason=f"Low-risk tool ({spec.risk})."
    )
    _audit(verdict)
    return verdict


def _audit(verdict: PolicyDecision) -> None:
    log.info(
        "policy.decision",
        tool=verdict.tool,
        decision=verdict.decision,
        reason=verdict.reason,
    )
