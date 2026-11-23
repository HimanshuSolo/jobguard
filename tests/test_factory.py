"""Tests for the tiered client factory (no network, no credentials)."""

from __future__ import annotations

import pytest

from jobguard.agent.ratelimit import RateLimiter
from jobguard.config import Settings
from jobguard.llm.factory import build_fast_client
from jobguard.llm.fake import FakeClient


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"llm_provider": "openai_compat"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_fast_client_uses_fast_model_when_configured() -> None:
    settings = _settings(
        llm_base_url="https://api.example.com/v1",
        llm_api_key="k",
        llm_model="big-model",
        llm_fast_model="small-model",
        llm_max_requests_per_min=60.0,
        llm_max_tokens_per_min=6000.0,
    )
    from jobguard.llm.factory import build_pipeline_clients

    smart, fast, limiter = build_pipeline_clients(settings)
    assert isinstance(limiter, RateLimiter)
    assert getattr(fast, "_model", None) == "small-model"
    assert getattr(smart, "_model", None) == "big-model"
    # One shared limiter instance across both stacks.
    assert getattr(fast, "_limiter", None) is limiter


def test_fast_client_defaults_to_main_model() -> None:
    settings = _settings(
        llm_base_url="https://api.example.com/v1",
        llm_api_key="k",
        llm_model="only-model",
        llm_max_requests_per_min=60.0,
        llm_max_tokens_per_min=6000.0,
    )
    fast = build_fast_client(settings)
    assert getattr(fast, "_model", None) == "only-model"


def test_fake_provider_needs_no_credentials() -> None:
    settings = Settings(llm_provider="fake")
    assert isinstance(build_fast_client(settings), FakeClient)


def test_unknown_provider_rejected() -> None:
    with pytest.raises(ValueError):
        build_fast_client(_settings())
