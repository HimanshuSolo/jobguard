"""Tests for Settings: defaults and the DEMO_MODE live-credential guard."""

from __future__ import annotations

import pytest

from jobguard.config import DemoModeError, Settings


def test_defaults_are_synthetic_safe() -> None:
    settings = Settings()
    assert settings.owner_email == "owner@example.com"
    assert settings.demo_mode is False
    assert settings.llm_provider == "fake"
    assert settings.is_smtp_configured is False


def test_demo_mode_blocks_live_credentials() -> None:
    settings = Settings(demo_mode=True)
    with pytest.raises(DemoModeError):
        settings.require_live_credentials()


def test_live_credentials_allowed_when_not_demo() -> None:
    settings = Settings(demo_mode=False)
    settings.require_live_credentials()  # must not raise


def test_llm_throttle_defaults() -> None:
    settings = Settings()
    assert settings.llm_max_requests_per_min == 20.0
    assert settings.llm_max_tokens_per_min == 8000.0
    assert settings.llm_max_tokens_per_run == 20000
    assert settings.llm_top_n_per_run == 30
    assert settings.llm_cheap_score_min == 15.0


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNER_EMAIL", "me@example.com")
    monkeypatch.setenv("DEMO_MODE", "true")
    settings = Settings()
    assert settings.owner_email == "me@example.com"
    assert settings.demo_mode is True
