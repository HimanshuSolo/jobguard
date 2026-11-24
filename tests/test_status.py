"""Tests for per-item pipeline status, honest progress, and schema patching."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from jobguard.ingestion import backfill_statuses, summarize
from jobguard.store import Posting, Suggestion, ensure_columns, init_db


def _posting(session: Session, **kwargs: object) -> Posting:
    from jobguard.ingestion import canonical_key

    company = str(kwargs.pop("company", "Acme"))
    role = str(kwargs.pop("role", "SDE Intern"))
    row = Posting(
        company=company,
        role=role,
        canonical_key=canonical_key(company, role, "", "") + str(kwargs.pop("key", "")),
        **kwargs,  # type: ignore[arg-type]
    )
    session.add(row)
    session.commit()
    return row


def test_backfill_and_summarize() -> None:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        scored = _posting(session, key="a")
        session.add(Suggestion(posting_id=scored.id, card={}, resume_item_ids=[], gaps=[]))
        _posting(session, key="b", scam_flag=True)
        _posting(session, key="c", deadline=datetime.now(UTC) - timedelta(days=1))
        _posting(session, key="d")
        session.commit()
        counts = backfill_statuses(session)
        assert counts == {"done": 1, "flagged_scam": 1, "expired": 1}
        assert backfill_statuses(session) == {}  # second run is a no-op
        report = summarize(session)
        assert report["total"] == 4
        assert report["scored"] == 1
        assert report["queued"] == 1
        assert report["expired"] == 1
        assert report["suggestions"] == 1
        assert report["eligible"] == 2  # scored + queued only


def test_terminal_statuses_never_count_as_queued() -> None:
    from jobguard.ingestion import TERMINAL_STATUSES

    assert "done" in TERMINAL_STATUSES
    assert "queued" not in TERMINAL_STATUSES


def test_ensure_columns_patches_legacy_table(tmp_path: object) -> None:
    from pathlib import Path

    db = Path(str(tmp_path)) / "legacy.db"
    old_engine = create_engine(f"sqlite:///{db}")
    with old_engine.begin() as conn:
        from sqlalchemy import text as _text

        conn.execute(
            _text(
                "CREATE TABLE postings (id INTEGER PRIMARY KEY, company VARCHAR(256), "
                "role VARCHAR(256), location VARCHAR(256), deadline DATETIME, "
                "link TEXT, skills JSON, eligibility TEXT, source VARCHAR(128), "
                "sender VARCHAR(512), body TEXT, arrived_at DATETIME, "
                "seen_on JSON, canonical_key VARCHAR(128), score FLOAT, "
                "scam_flag BOOLEAN, status VARCHAR(32))"
            )
        )
    added = ensure_columns(create_engine(f"sqlite:///{db}"))
    assert sorted(added) == [
        "postings.cheap_score",
        "postings.pipe_stage",
        "postings.pipe_status",
        "postings.posted_at",
    ]
    assert ensure_columns(create_engine(f"sqlite:///{db}")) == []  # idempotent


def test_status_cli_reports_counts(tmp_path: object, capsys: pytest.CaptureFixture[str]) -> None:
    import os

    from jobguard.cli import main
    from jobguard.config import get_settings

    db = tmp_path / "status.db"  # type: ignore[operator]
    os.environ["DATABASE_URL"] = f"sqlite:///{db}"
    get_settings.cache_clear()
    try:
        assert main(["init-db"]) == 0
        assert main(["status"]) == 0
    finally:
        del os.environ["DATABASE_URL"]
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert "total=" in out and "queued=" in out
