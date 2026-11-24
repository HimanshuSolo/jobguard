"""Engine/session helpers for the JobGuard database."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from jobguard.store.models import Base

# Additive, idempotent schema evolution: (table, column, SQL type) applied
# with ALTER TABLE only when the column is missing. No data migration, no
# drops, backend-agnostic (SQLite + Postgres).
_SCHEMA_PATCHES: list[tuple[str, str, str]] = [
    ("postings", "pipe_status", "VARCHAR(32) DEFAULT 'queued'"),
    ("postings", "pipe_stage", "VARCHAR(32) DEFAULT 'none'"),
    ("postings", "cheap_score", "FLOAT DEFAULT 0.0"),
    ("postings", "posted_at", "TIMESTAMP"),
]


def get_engine(database_url: str) -> Engine:
    """Create a SQLAlchemy engine for SQLite or Postgres URLs."""
    return create_engine(database_url)


def ensure_columns(engine: Engine) -> list[str]:
    """Add missing columns from _SCHEMA_PATCHES; returns what was added."""
    added: list[str] = []
    with engine.begin() as conn:
        existing = {
            table: {col["name"] for col in inspect(conn).get_columns(table)}
            for table in {table for table, _, _ in _SCHEMA_PATCHES}
            if inspect(conn).has_table(table)
        }
        for table, column, sqltype in _SCHEMA_PATCHES:
            if table in existing and column not in existing[table]:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sqltype}"))
                added.append(f"{table}.{column}")
    return added


def init_db(engine: Engine) -> None:
    """Create all tables (no-op for existing tables), then patch columns."""
    Base.metadata.create_all(engine)
    ensure_columns(engine)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a session factory bound to ``engine``."""
    return sessionmaker(bind=engine, expire_on_commit=False)
