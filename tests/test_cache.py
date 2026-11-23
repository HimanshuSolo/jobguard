"""LLM cache + per-stage checkpoint tests (fake LLM, file DB, no sleeping).

The cache key is sha256(model family, prompt version, cleaned content). A
hit makes zero network calls and skips the token budget; entries persist in
the ``llm_cache`` table so interrupted runs resume without duplicates.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from jobguard.agent import Profile, TokenBudget, run_pipeline
from jobguard.agent import pipeline as pipeline_mod
from jobguard.ingestion import canonical_key
from jobguard.llm.cache import cache_key
from jobguard.llm.fake import FakeClient
from jobguard.resume import Resume, ResumeItem
from jobguard.store import LLMCache, Posting, Suggestion, init_db, session_factory


def _sessions(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{tmp_path}/cache.db")
    init_db(engine)
    return session_factory(engine)


def _env() -> tuple[Profile, Resume]:
    return (
        Profile(skills=["Python"], role_types=["SDE"], locations=["Remote"]),
        Resume(owner="T", items=[ResumeItem(id="exp-1", title="Intern")]),
    )


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


def _posting(session: Session, idx: int, body_company: str = "Acme") -> Posting:
    """Template-parsed row: only the match+suggest stage makes an LLM call."""
    row = Posting(
        company=body_company,
        role="SDE Intern",
        link="https://acme.example/j",
        canonical_key=canonical_key(body_company, f"SDE Intern {idx}", "", ""),
        pipe_status="queued",
    )
    session.add(row)
    session.commit()
    return row


def _body() -> str:
    return "SDE Intern at Acme. Python role. Apply at https://acme.example/j"


def test_cache_key_differs_by_family_version_content() -> None:
    base = cache_key("fam", "v1", "[match_suggest]\nx")
    assert base == cache_key("fam", "v1", "[match_suggest]\nx")  # deterministic
    assert len(base) == 64
    assert base != cache_key("other", "v1", "[match_suggest]\nx")
    assert base != cache_key("fam", "v2", "[match_suggest]\nx")
    assert base != cache_key("fam", "v1", "[classify]\nx")


def test_cache_hit_makes_zero_calls(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    profile, resume = _env()
    with sessions() as session:
        first = _posting(session, 0)
        result = run_pipeline(
            first,
            body=_body(),
            sender="jobs-noreply@linkedin.com",
            client=_match_client(),
            profile=profile,
            resume=resume,
            session=session,
        )
        assert result.status == "suggested"

        second_client = _match_client()
        second = _posting(session, 1)
        result2 = run_pipeline(
            second,
            body=_body(),
            sender="jobs-noreply@linkedin.com",
            client=second_client,
            profile=profile,
            resume=resume,
            session=session,
        )
        assert result2.status == "suggested"
        assert second_client.calls == []
        assert session.query(Suggestion).count() == 2


def test_prompt_version_change_invalidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = _sessions(tmp_path)
    profile, resume = _env()
    with sessions() as session:
        run_pipeline(
            _posting(session, 0),
            body=_body(),
            sender="jobs-noreply@linkedin.com",
            client=_match_client(),
            profile=profile,
            resume=resume,
            session=session,
        )
        monkeypatch.setattr(pipeline_mod, "PROMPT_VERSION", pipeline_mod.PROMPT_VERSION + ".test")
        uncached = _match_client()
        result = run_pipeline(
            _posting(session, 1),
            body=_body(),
            sender="jobs-noreply@linkedin.com",
            client=uncached,
            profile=profile,
            resume=resume,
            session=session,
        )
        assert result.status == "suggested"
        assert len(uncached.calls) == 1


def test_resume_after_failure_without_duplicates(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    profile, resume = _env()
    with sessions() as session:
        post = _posting(session, 0)
        bad = run_pipeline(
            post,
            body=_body(),
            sender="jobs-noreply@linkedin.com",
            client=FakeClient(default="not json"),  # match never parses
            profile=profile,
            resume=resume,
            session=session,
        )
        assert bad.status == "skipped"
        assert post.pipe_status == "failed"

        good = _match_client()
        stl = run_pipeline(
            post,
            body=_body(),
            sender="jobs-noreply@linkedin.com",
            client=good,
            profile=profile,
            resume=resume,
            session=session,
        )
        assert stl.status == "suggested"
        assert len(good.calls) == 1  # match retried live; nothing else re-called
        assert session.query(Suggestion).count() == 1


def test_cache_hit_skips_budget_charge(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    profile, resume = _env()
    with sessions() as session:
        run_pipeline(
            _posting(session, 0),
            body=_body(),
            sender="jobs-noreply@linkedin.com",
            client=_match_client(),
            profile=profile,
            resume=resume,
            session=session,
            budget=TokenBudget(max_tokens=100_000),
        )
        tiny = TokenBudget(max_tokens=1)
        result = run_pipeline(
            _posting(session, 1),
            body=_body(),
            sender="jobs-noreply@linkedin.com",
            client=_match_client(),
            profile=profile,
            resume=resume,
            session=session,
            budget=tiny,
        )
        assert result.status == "suggested"
        assert tiny.used_tokens == 0
        assert session.query(LLMCache).count() == 1
