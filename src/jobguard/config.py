"""Application configuration.

Single source of truth for settings. Values come from the environment (or a
local ``.env`` file); nothing secret is ever hard-coded. See ``.env.example``.

Hard rule enforced here: when ``DEMO_MODE`` is on, only synthetic data may be
used, so any attempt to use live credentials must fail fast — call
:meth:`Settings.require_live_credentials` before touching Gmail, SMTP, or a
hosted LLM.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DemoModeError(RuntimeError):
    """Raised when live credentials are requested while DEMO_MODE is on."""


class Settings(BaseSettings):
    """All JobGuard settings (env prefix: none, ``.env`` file supported)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(
        default="sqlite:///./data/jobguard.local.db",
        description="SQLAlchemy URL. SQLite locally, Postgres via DATABASE_URL in cloud.",
    )
    owner_email: str = Field(
        default="owner@example.com",
        description="Hard-locked recipient of every outgoing email.",
    )
    demo_mode: bool = Field(
        default=False,
        description="When true, load synthetic data only; live credentials are unusable.",
    )

    llm_provider: str = Field(default="fake", description="fake | openai_compat")
    llm_base_url: str = Field(default="", description="OpenAI-compatible base URL.")
    llm_api_key: str = Field(default="", description="API key for the LLM provider.")
    llm_model: str = Field(default="", description="Model name for the LLM provider.")
    llm_max_tokens_per_run: int = Field(default=20000, ge=1)
    llm_max_requests_per_run: int = Field(
        default=100, gt=0, le=10000, description="Max LLM calls per worker run."
    )
    llm_top_n_per_run: int = Field(
        default=30, gt=0, description="Max postings analyzed with the LLM per run."
    )
    llm_cheap_score_min: float = Field(
        default=15.0,
        ge=0.0,
        le=100.0,
        description="Minimum cheap heuristic score for LLM analysis.",
    )
    llm_workers: int = Field(
        default=2, gt=0, le=8, description="Worker pool size for the LLM stage."
    )
    llm_max_input_chars: int = Field(
        default=3000, gt=0, description="Max input chars sent to the LLM per call."
    )
    llm_fast_model: str = Field(
        default="",
        description="Cheaper model for classify/extract; empty = use llm_model.",
    )
    llm_secondary_base_url: str = ""
    llm_secondary_api_key: str = ""
    llm_secondary_model: str = ""
    llm_circuit_threshold: int = Field(default=3, ge=1)
    llm_max_requests_per_min: float = Field(
        default=20.0, gt=0, description="Shared token-bucket request rate."
    )
    llm_max_tokens_per_min: float = Field(
        default=8000.0, gt=0, description="Shared token-bucket token rate."
    )
    llm_replay_path: str = Field(default="", description="JSONL record/replay file (CI).")
    llm_replay_mode: Literal["record", "replay"] = "record"

    # --- Phase 4: LLM pipeline ---
    resume_path: str = Field(default="data/private/resume.yaml")
    profile_path: str = Field(default="data/profile.yaml")

    # --- Phase 5: digest + notifications ---
    digest_best_score: float = Field(default=75.0, ge=0.0, le=100.0)
    digest_urgent_hours: int = Field(default=48, ge=1)
    sender_max_per_day: int = Field(default=20, ge=1)
    approve_base_url: str = Field(default="http://localhost:8000")

    # --- Phase 6: dashboard ---
    dashboard_username: str = ""
    dashboard_secret_key: str = ""
    dashboard_password_hash: str = Field(
        default="", description="SHA-256 hex of the dashboard password."
    )

    sender_smtp_host: str = ""
    sender_smtp_port: int = 587
    sender_smtp_user: str = ""
    sender_smtp_password: str = ""
    sender_from_address: str = ""

    # --- Phase 2: sources & ingestion ---
    prefilter_keywords: str = Field(
        default="sde,software,developer,engineer,intern,backend,frontend,"
        "full-stack,full stack,devops,data,ml,android,ios,qa,sdet",
        description="Comma-separated substrings; a posting must match one to survive.",
    )
    prefilter_senders: str = Field(
        default="linkedin,indeed,naukri,internshala,unstop,glassdoor",
        description="Comma-separated substrings of accepted mail alert senders.",
    )
    boards_config_path: str = Field(
        default="data/boards.yaml",
        description="Company board list (see data/boards.example.yaml).",
    )
    rss_feeds: str = Field(
        default="",
        description="Comma-separated 'name::url' feeds for the RSS adapter.",
    )
    gmail_client_secret: str = Field(
        default="data/private/gmail_client_secret.json",
        description="OAuth client secret (gitignored). Absent = Gmail skipped.",
    )
    gmail_token_path: str = Field(
        default="data/private/gmail_token.json",
        description="OAuth token cache (gitignored).",
    )
    imap_host: str = Field(default="", description="IMAP hostname for the fallback.")
    imap_user: str = Field(default="", description="IMAP username for the fallback.")
    imap_password_file: str = Field(
        default="data/private/imap_password.txt",
        description="File holding the IMAP app password (gitignored).",
    )

    def require_live_credentials(self) -> None:
        """Fail fast if live credentials are requested in demo mode."""
        if self.demo_mode:
            raise DemoModeError(
                "DEMO_MODE is on: live credentials (Gmail/SMTP/hosted LLM) are disabled."
            )

    @property
    def is_smtp_configured(self) -> bool:
        """Whether digest-sending SMTP credentials are present."""
        return bool(self.sender_smtp_host and self.sender_smtp_user)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor for the process-wide settings object."""
    return Settings()
