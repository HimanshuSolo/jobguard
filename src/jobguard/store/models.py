"""Database models.

Tables mirror SPEC section 5. SQLite locally, Postgres in the cloud — both go
through SQLAlchemy so the models stay backend-agnostic. JSON columns carry the
list/blob fields (sources/seen_on, cards, plans); Postgres stores them as JSON
natively, SQLite as TEXT — no backend-specific code here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Timezone-aware UTC now (naive ``datetime.utcnow`` is deprecated)."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for all JobGuard tables."""


class EmailSeen(Base):
    """Gmail/IMAP message IDs already processed (idempotent catch-up)."""

    __tablename__ = "emails_seen"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    sender: Mapped[str] = mapped_column(String(512), default="")
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RawPosting(Base):
    """Un-normalized payload exactly as a source adapter returned it."""

    __tablename__ = "raw_postings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(128), index=True)
    external_id: Mapped[str] = mapped_column(String(512), default="")
    payload: Mapped[str] = mapped_column(Text, default="{}")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Posting(Base):
    """Normalized, deduped job posting."""

    __tablename__ = "postings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    company: Mapped[str] = mapped_column(String(256), index=True)
    role: Mapped[str] = mapped_column(String(256))
    location: Mapped[str] = mapped_column(String(256), default="")
    deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    link: Mapped[str] = mapped_column(Text, default="")
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    eligibility: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(128), default="")
    sender: Mapped[str] = mapped_column(String(512), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    arrived_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    seen_on: Mapped[list[str]] = mapped_column(JSON, default=list)
    canonical_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    scam_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="new")
    # Pipeline state machine (Phase 8; distinct from the user-facing status
    # above). Terminal states are never re-processed by later runs.
    pipe_status: Mapped[str] = mapped_column(String(32), default="queued")
    pipe_stage: Mapped[str] = mapped_column(String(32), default="none")
    cheap_score: Mapped[float] = mapped_column(Float, default=0.0)


class Suggestion(Base):
    """Per-posting resume-tailoring card."""

    __tablename__ = "suggestions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    posting_id: Mapped[int] = mapped_column(ForeignKey("postings.id"), index=True)
    card: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    resume_item_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    gaps: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LLMCache(Base):
    """One validated LLM response, keyed by sha256(family, version, content).

    Hits make zero network calls and skip the token budget. Entries survive
    restarts, so interrupted runs resume without duplicate calls.
    """

    __tablename__ = "llm_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_family: Mapped[str] = mapped_column(String(256), index=True, default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    response_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PendingAction(Base):
    """Risky plan awaiting human approval. Only token *hashes* are stored."""

    __tablename__ = "pending_actions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    plan: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    risk: Mapped[str] = mapped_column(String(32), default="high")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DigestSent(Base):
    """Record of sent digests so items are never re-sent."""

    __tablename__ = "digests_sent"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    digest_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    items: Mapped[list[str]] = mapped_column(JSON, default=list)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    """Every guardrail trigger, policy decision, approval, and LLM call metadata.

    Never store message content here — metadata only (tool names, decisions,
    model names, token counts, latency).
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event: Mapped[str] = mapped_column(String(128), index=True)
    detail: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RunState(Base):
    """Per-source cursor / last processed ID for idempotent catch-up runs."""

    __tablename__ = "run_state"

    source: Mapped[str] = mapped_column(String(128), primary_key=True)
    cursor: Mapped[str] = mapped_column(String(512), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
