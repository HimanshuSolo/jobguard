"""Pytest bootstrap: make `jobguard` (src layout) and top-level `evals` importable."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for entry in (str(ROOT / "src"), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)


@pytest.fixture(autouse=True)
def _isolate_settings_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests hermetic: ignore the developer's real `.env` and environ.

    Every `Settings()` built inside a test sees an empty env file and none
    of the app's env vars, so local secrets can never leak into (or break)
    assertions. Runtime behavior outside tests is unchanged.
    """
    from jobguard.config import Settings, get_settings

    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        Settings,
        "model_config",
        {**Settings.model_config, "env_file": str(empty)},
    )
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    # The default Gmail paths point at data/private/, which holds a real
    # token on a developer machine; a token alone now enables Gmail.
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(tmp_path / "no-gmail-token.json"))
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", str(tmp_path / "no-gmail-secret.json"))
    get_settings.cache_clear()
