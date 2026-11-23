"""Tests for deterministic ranking, fallback, budget, and circuit breaker."""

from __future__ import annotations

import pytest

from jobguard.agent import (
    BudgetExhausted,
    CircuitBreaker,
    FallbackClient,
    Profile,
    TokenBudget,
    load_profile,
    score_posting,
)
from jobguard.llm.client import LLMError, LLMResponse
from jobguard.llm.fake import FakeClient


def _profile() -> Profile:
    return Profile(
        skills=["Python", "SQL"],
        role_types=["SDE", "Backend"],
        locations=["Remote", "Bengaluru"],
        new_grad=True,
    )


def test_rank_orders_fit() -> None:
    profile = _profile()
    good = score_posting(
        profile, role="SDE Intern", location="Remote", skills=["Python"], text="Python SQL role"
    )
    bad = score_posting(
        profile, role="Marketing Manager", location="Mumbai", skills=[], text="Brand campaigns"
    )
    assert good.score > bad.score
    assert any("skill overlap" in r for r in good.reasons)
    assert 0.0 <= bad.score <= 100.0


def test_rank_deadline_bonus_and_bad_date() -> None:
    profile = _profile()
    soon = score_posting(
        profile, role="X", location="", skills=[], text="", deadline="2026-12-01T00:00:00+00:00"
    )
    assert any("deadline" in r for r in soon.reasons)
    broken = score_posting(
        profile, role="X", location="", skills=[], text="", deadline="not-a-date"
    )
    assert broken.score == 0.0


def test_load_profile_example() -> None:
    profile = load_profile("data/profile.example.yaml")
    assert "Python" in profile.skills
    assert profile.new_grad is True


def test_fallback_uses_secondary() -> None:
    class Boom:
        @property
        def name(self) -> str:
            return "boom"

        def complete(self, *, system: str, user: str) -> LLMResponse:
            raise LLMError("primary down")

    client = FallbackClient(Boom(), FakeClient(default="fine"))  # type: ignore[arg-type]
    assert client.complete(system="s", user="u").text == "fine"


def test_fallback_all_fail() -> None:
    class Boom:
        @property
        def name(self) -> str:
            return "boom"

        def complete(self, *, system: str, user: str) -> LLMResponse:
            raise LLMError("down")

    with pytest.raises(LLMError):
        FallbackClient(Boom(), Boom()).complete(system="s", user="u")  # type: ignore[arg-type]


def test_fallback_needs_secondary() -> None:
    with pytest.raises(ValueError):
        FallbackClient(FakeClient())


def test_budget_exhaustion() -> None:
    budget = TokenBudget(max_tokens=1)
    with pytest.raises(BudgetExhausted):
        budget.charge(system="s", user="u" * 100, response=LLMResponse(model="m", text="t"))


def test_budget_charges_reported_tokens() -> None:
    budget = TokenBudget(max_tokens=100)
    budget.charge(
        system="s",
        user="u",
        response=LLMResponse(model="m", text="t", input_tokens=10, output_tokens=5),
    )
    assert budget.used_tokens == 15


def test_breaker_opens_and_recovers() -> None:
    now = [1000.0]
    breaker = CircuitBreaker(FakeClient(), threshold=2, cooldown_s=60.0, _clock=lambda: now[0])
    assert breaker.is_open is False
    # FakeClient never fails; force failures via a failing inner.
    calls = {"n": 0}

    class Flaky:
        @property
        def name(self) -> str:
            return "flaky"

        def complete(self, *, system: str, user: str) -> LLMResponse:
            calls["n"] += 1
            raise LLMError("flaky down")

    flaky_breaker = CircuitBreaker(Flaky(), threshold=2, cooldown_s=60.0, _clock=lambda: now[0])  # type: ignore[arg-type]
    with pytest.raises(LLMError):
        flaky_breaker.complete(system="s", user="u")
    with pytest.raises(LLMError):
        flaky_breaker.complete(system="s", user="u")
    assert flaky_breaker.is_open is True
    with pytest.raises(LLMError, match="circuit open"):
        flaky_breaker.complete(system="s", user="u")
    assert calls["n"] == 2  # open circuit made no third call
    now[0] += 61.0
    assert flaky_breaker.is_open is False
