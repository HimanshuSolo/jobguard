"""Tests for DB models + init (SQLite in-memory; Postgres uses the same models)."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from jobguard.store import (
    AuditLog,
    Posting,
    RunState,
    get_engine,
    init_db,
    session_factory,
)


@pytest.fixture()
def engine() -> Engine:
    eng = create_engine("sqlite:///:memory:")
    init_db(eng)
    return eng


def test_get_engine_sqlite() -> None:
    eng = get_engine("sqlite:///:memory:")
    assert eng is not None


def test_get_engine_postgres_dialect() -> None:
    # No connection is made; this only proves Neon-style URLs resolve
    # to the psycopg dialect.
    eng = get_engine("postgresql://user:pass@ep-test.aws.neon.tech/db?sslmode=require")
    assert eng.dialect.name == "postgresql"


def test_posting_roundtrip(engine: Engine) -> None:
    sessions = session_factory(engine)
    with sessions() as session:
        session.add(
            Posting(
                company="Acme",
                role="SDE Intern",
                location="Remote",
                link="https://example.com/jobs/1",
                skills=["Python"],
                seen_on=["linkedin-alert"],
                canonical_key="abc123",
            )
        )
        session.commit()
    with sessions() as session:
        row = session.query(Posting).filter_by(canonical_key="abc123").one()
        assert row.company == "Acme"
        assert row.status == "new"
        assert row.scam_flag is False
        assert row.skills == ["Python"]


def test_canonical_key_unique(engine: Engine) -> None:
    sessions = session_factory(engine)
    with sessions() as session:
        session.add(Posting(company="Acme", role="SDE Intern", canonical_key="dup"))
        session.commit()
        session.add(Posting(company="Acme", role="SDE Intern", canonical_key="dup"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_audit_log_metadata_only(engine: Engine) -> None:
    sessions = session_factory(engine)
    with sessions() as session:
        session.add(AuditLog(event="policy.decision", detail={"tool": "send_email"}))
        session.commit()
    with sessions() as check:
        assert check.query(AuditLog).count() == 1


def test_run_state_cursor(engine: Engine) -> None:
    with Session(engine) as session:
        session.add(RunState(source="gmail", cursor="msg-42"))
        session.commit()
        state = session.get(RunState, "gmail")
        assert state is not None
        assert state.cursor == "msg-42"
