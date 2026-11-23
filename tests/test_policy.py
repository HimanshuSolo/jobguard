"""Tests for the deterministic policy registry (one case per tool rule)."""

from __future__ import annotations

from jobguard.policy import PolicyContext, evaluate

OWNER = "owner@example.com"


def test_send_email_to_owner_allowed() -> None:
    verdict = evaluate(PolicyContext(tool="send_email", recipient=OWNER, owner_email=OWNER))
    assert verdict.decision == "allow"


def test_send_email_to_anyone_else_denied() -> None:
    verdict = evaluate(
        PolicyContext(tool="send_email", recipient="attacker@evil.example", owner_email=OWNER)
    )
    assert verdict.decision == "deny"


def test_send_email_over_cap_denied() -> None:
    verdict = evaluate(
        PolicyContext(
            tool="send_email",
            recipient=OWNER,
            owner_email=OWNER,
            within_rate_limits=False,
        )
    )
    assert verdict.decision == "deny"


def test_submit_application_always_denied() -> None:
    assert evaluate(PolicyContext(tool="submit_application")).decision == "deny"


def test_update_master_resume_needs_approval() -> None:
    assert evaluate(PolicyContext(tool="update_master_resume")).decision == "needs_approval"


def test_fetch_job_url_requires_allowlist_and_ssrf() -> None:
    assert evaluate(PolicyContext(tool="fetch_job_url")).decision == "deny"
    assert (
        evaluate(
            PolicyContext(
                tool="fetch_job_url",
                domain_allowlisted=True,
                ssrf_checks_passed=False,
            )
        ).decision
        == "deny"
    )
    assert (
        evaluate(
            PolicyContext(
                tool="fetch_job_url",
                domain_allowlisted=True,
                ssrf_checks_passed=True,
            )
        ).decision
        == "allow"
    )


def test_fetch_feed_requires_allowlist() -> None:
    assert evaluate(PolicyContext(tool="fetch_feed")).decision == "deny"
    assert evaluate(PolicyContext(tool="fetch_feed", domain_allowlisted=True)).decision == "allow"


def test_low_risk_tools_allowed() -> None:
    for tool in ("read_mail", "write_db", "build_digest"):
        assert evaluate(PolicyContext(tool=tool)).decision == "allow"


def test_unknown_tool_denied() -> None:
    assert evaluate(PolicyContext(tool="rm_rf_everything")).decision == "deny"
