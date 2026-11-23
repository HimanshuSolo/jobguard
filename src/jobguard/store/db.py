"""Engine/session helpers for the JobGuard database."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from jobguard.store.models import Base


def get_engine(database_url: str) -> Engine:
    """Create a SQLAlchemy engine for SQLite or Postgres URLs."""
    return create_engine(database_url)


def init_db(engine: Engine) -> None:
    """Create all tables (no-op for existing tables)."""
    Base.metadata.create_all(engine)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a session factory bound to ``engine``."""
    return sessionmaker(bind=engine, expire_on_commit=False)
