"""Tests for `jobguard run-worker` wiring (no network, no real credentials)."""

from __future__ import annotations

import pytest

from jobguard.cli import main
from jobguard.config import get_settings


def test_run_worker_nothing_configured(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    from pathlib import Path

    db = Path(str(tmp_path)) / "w.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("BOARDS_CONFIG_PATH", str(Path(str(tmp_path)) / "nope.yaml"))
    monkeypatch.setenv("RSS_FEEDS", "")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", str(Path(str(tmp_path)) / "nope.json"))
    monkeypatch.setenv("IMAP_HOST", "")
    monkeypatch.setenv("DEMO_MODE", "false")
    get_settings.cache_clear()
    try:
        assert main(["run-worker"]) == 0
    finally:
        get_settings.cache_clear()


def test_run_worker_demo_mode_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_MODE", "true")
    get_settings.cache_clear()
    try:
        assert main(["run-worker"]) == 0
    finally:
        get_settings.cache_clear()
