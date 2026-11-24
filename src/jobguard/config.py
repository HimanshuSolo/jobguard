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
    digest_recipients: str = Field(
        default="",
        description="Extra digest recipients (comma-separated). Each gets a separate "
        "email with the job list only: no resume notes, no approvals.",
    )
    approve_base_url: str = Field(default="http://localhost:8000")

    # --- Phase 6: dashboard ---
    dashboard_username: str = ""
    dashboard_secret_key: str = ""
    dashboard_password_hash: str = Field(
        default="", description="SHA-256 hex of the dashboard password."
    )
    # Clerk sign-in (replaces the password login when the publishable key is set).
    clerk_publishable_key: str = Field(default="", description="pk_test_... / pk_live_...")
    clerk_secret_key: str = Field(
        default="", description="sk_... (server only): used to look up the signed-in email."
    )
    clerk_allowed_emails: str = Field(
        default="", description="Comma-separated emails allowed in; empty = OWNER_EMAIL."
    )
    clerk_allowed_user_ids: str = Field(
        default="", description="Comma-separated Clerk user ids allowed in (no secret needed)."
    )
    clerk_authorized_parties: str = Field(
        default="",
        description="Comma-separated origins allowed as the token's azp, e.g. "
        "https://jobguard.onrender.com (empty = not checked).",
    )

    sender_smtp_host: str = ""
    sender_smtp_port: int = 587
    sender_smtp_user: str = ""
    sender_smtp_password: str = ""
    sender_from_address: str = ""

    # --- Phase 2: sources & ingestion ---
    prefilter_keywords: str = Field(
        default="sde,sdet,software,developer,engineer,engineering,programmer,backend,"
        "frontend,full stack,full-stack,fullstack,devops,sre,web,mobile,android,"
        "ios,flutter,react,node,python,java,golang,rust,ml,ai,machine learning,"
        "data,cloud,qa,test automation",
        description="Comma-separated whole words; the job TITLE must contain one to survive.",
    )
    prefilter_senders: str = Field(
        default="linkedin,indeed,naukri,internshala,unstop,glassdoor,hirist,instahyre,"
        "wellfound,cutshort,foundit,apna",
        description="Comma-separated substrings of accepted mail alert senders.",
    )
    location_scope: Literal["india", "any"] = Field(
        default="india",
        description="'india' keeps India, India-compatible remote and location-less "
        "postings only; 'any' disables the location gate.",
    )
    role_level: Literal["intern", "any"] = Field(
        default="intern",
        description="'intern' keeps only internship/apprentice/trainee titles.",
    )
    grad_year: int = Field(
        default=2027,
        ge=0,
        le=2100,
        description="Your graduation batch; postings naming only other batches are "
        "dropped (0 disables).",
    )
    gmail_query: str = Field(
        default="",
        description="Gmail search for job mail; empty = built-in alert-sender/subject query.",
    )
    gmail_lookback_days: int = Field(
        default=3, ge=1, le=60, description="Window for the first/reset Gmail sync."
    )
    gmail_max_messages: int = Field(
        default=300, ge=1, le=2000, description="Max messages fetched per Gmail sync."
    )
    adzuna_app_id: str = Field(default="", description="Adzuna API app id (optional source).")
    adzuna_app_key: str = Field(default="", description="Adzuna API key (optional source).")
    adzuna_queries: str = Field(
        default="software engineer intern,sde intern,software developer intern,"
        "developer intern,software engineer fresher,backend developer,"
        "frontend developer,full stack developer",
        description="Comma-separated Adzuna India search phrases.",
    )
    adzuna_max_days_old: int = Field(default=3, ge=1, le=30)
    digest_max_items: int = Field(
        default=60, ge=1, le=500, description="Max new jobs listed in one digest email."
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
