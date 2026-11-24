"""On-demand analyze tests (fake LLM, file DB, no sleeping, no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from jobguard.agent import analyze as analyze_mod
from jobguard.agent.analyze import analyze_posting
from jobguard.config import Settings
from jobguard.ingestion import canonical_key
from jobguard.llm.fake import FakeClient
from jobguard.store import Posting, Suggestion, init_db, session_factory


def _match_client() -> FakeClient:
    return FakeClient(
        rules=[
            (
                "[match_suggest]",
                '{"score": 82, "reasons": ["good fit"], "resume_item_ids": ["exp-1"],'
                ' "posting_key": "k", "suggestions": [{"kind": "reword",'
                ' "resume_item_ids": ["exp-1"], "text": "Emphasize FastAPI."}],'
                ' "gaps": []}',
            ),
        ]
    )


def _files(tmp_path: Path) -> tuple[str, str]:
    profile = tmp_path / "profile.yaml"
    profile.write_text("skills: [Python]\nrole_types: [SDE]\nlocations: [Remote]\n")
    resume = tmp_path / "resume.yaml"
    resume.write_text(
        "owner: T\nheadline: h\n"
        "experience:\n  - id: exp-1\n    title: Intern\n    org: Acme\n"
        '    start: "2024-06"\n    end: "2024-08"\n    bullets: [Did Python things]\n'
    )
    return str(profile), str(resume)


def _settings(tmp_path: Path, provider: str = "fake") -> Settings:
    profile_path, resume_path = _files(tmp_path)
    return Settings(
        database_url=f"sqlite:///{tmp_path}/analyze.db",
        llm_provider=provider,  # type: ignore[arg-type]
        profile_path=profile_path,
        resume_path=resume_path,
    )


def _sessions(settings: Settings) -> sessionmaker[Session]:
    engine = create_engine(settings.database_url)
    init_db(engine)
    return session_factory(engine)


def _post(
    session: Session,
    idx: int,
    *,
    status: str = "queued",
    deadline: datetime | None = None,
) -> int:
    row = Posting(
        company="Acme",
        role="SDE Intern",
        link="https://acme.example/j",
        canonical_key=canonical_key("Acme", f"SDE Intern {idx}", "", ""),
        pipe_status=status,
        deadline=deadline,
    )
    session.add(row)
    session.commit()
    return row.id


def _body() -> str:
    return "SDE Intern at Acme. Python role. Apply at https://acme.example/j"


def test_analyze_queued_runs_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, provider="openai_compat")
    sessions = _sessions(settings)
    monkeypatch.setattr(
        analyze_mod,
        "build_pipeline_clients",
        lambda settings: (_match_client(), _match_client(), None),
    )
    with sessions() as session:
        pid = _post(session, 0)
    result = analyze_posting(pid, sessions=sessions, settings=settings)
    assert result.outcome == "analyzed"
    assert result.llm_calls >= 1
    with sessions() as session:
        assert session.get(Posting, pid).pipe_status == "done"  # type: ignore[union-attr]
        assert session.query(Suggestion).count() == 1


def test_analyze_refuses_fake_without_force(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    sessions = _sessions(settings)
    with sessions() as session:
        pid = _post(session, 0)
    result = analyze_posting(pid, sessions=sessions, settings=settings)
    assert result.outcome == "refused"
    assert result.llm_calls == 0
    with sessions() as session:
        assert session.get(Posting, pid).pipe_status == "queued"  # type: ignore[union-attr]


def test_analyze_missing_posting(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    sessions = _sessions(settings)
    result = analyze_posting(999_999, sessions=sessions, settings=settings, force=True)
    assert result.outcome == "not-found"


def test_analyze_terminal_reported_without_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expired items are marked expired with zero LLM calls (no force)."""
    settings = _settings(tmp_path, provider="openai_compat")
    sessions = _sessions(settings)
    monkeypatch.setattr(
        analyze_mod,
        "build_pipeline_clients",
        lambda settings: (_match_client(), _match_client(), None),
    )
    with sessions() as session:
        pid = _post(session, 0, deadline=datetime.now(UTC) - timedelta(hours=1))
    result = analyze_posting(pid, sessions=sessions, settings=settings)
    assert result.outcome == "already"
    assert result.status == "expired"
    assert result.llm_calls == 0
    with sessions() as session:
        assert session.get(Posting, pid).pipe_status == "expired"  # type: ignore[union-attr]


def test_analyze_force_reruns_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, provider="openai_compat")
    sessions = _sessions(settings)
    monkeypatch.setattr(
        analyze_mod,
        "build_pipeline_clients",
        lambda settings: (_match_client(), _match_client(), None),
    )
    with sessions() as session:
        pid = _post(session, 0, status="failed")
    result = analyze_posting(pid, sessions=sessions, settings=settings, force=True)
    assert result.outcome == "analyzed"
    assert result.llm_calls >= 1
