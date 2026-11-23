"""Tests for the bounded-concurrency worker pool (fake LLM + file DB).

No network, no real sleeping. SQLite needs a file DB here: each pool
thread opens its own session, and ``:memory:`` databases are private to
a single connection.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobguard.agent import Profile, TokenBudget
from jobguard.agent.fallback import BudgetExhausted, CircuitBreaker, FallbackClient
from jobguard.cli import _run_pool
from jobguard.ingestion import canonical_key
from jobguard.llm.client import LLMClient, LLMError, LLMResponse
from jobguard.llm.fake import FakeClient
from jobguard.resume import Resume, ResumeItem
from jobguard.store import Posting, Suggestion, init_db, session_factory


def _sessions(tmp_path: object) -> sessionmaker:  # type: ignore[type-arg]
    from pathlib import Path

    engine = create_engine(f"sqlite:///{Path(str(tmp_path))}/pool.db")
    init_db(engine)
    return session_factory(engine)


@pytest.fixture
def sessions(tmp_path) -> Iterator[sessionmaker]:  # type: ignore[type-arg]
    yield _sessions(tmp_path)


def _env() -> tuple[Profile, Resume]:
    return (
        Profile(skills=["Python"], role_types=["SDE"], locations=["Remote"]),
        Resume(owner="T", items=[ResumeItem(id="exp-1", title="Intern")]),
    )


def _client() -> FakeClient:
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


def _queue(sessions, n: int, **kwargs: object) -> list[int]:  # type: ignore[no-untyped-def]
    ids: list[int] = []
    with sessions() as session:
        for i in range(n):
            company = str(kwargs.pop("company", "Acme"))
            role = str(kwargs.pop("role", "SDE Intern"))
            link = str(kwargs.pop("link", "https://acme.example/j"))
            row = Posting(
                company=company,
                role=role,
                canonical_key=canonical_key(company, f"{role} {i}", "", ""),
                link=link,
                pipe_status="queued",
                **kwargs,  # type: ignore[arg-type]
            )
            session.add(row)
            session.commit()
            ids.append(row.id)
    return ids


def test_pool_scores_queued_items(sessions) -> None:  # type: ignore[no-untyped-def]
    profile, resume = _env()
    ids = _queue(sessions, 3)
    counts = _run_pool(
        session_factory=sessions,
        posting_ids=ids,
        client=_client(),
        fast_client=_client(),
        profile=profile,
        resume=resume,
        budget=TokenBudget(max_tokens=100_000),
        max_input_chars=3000,
        max_workers=2,
    )
    assert sum(counts.values()) == 3
    with sessions() as session:
        assert session.query(Suggestion).count() == 3
        assert session.query(Posting).filter(Posting.pipe_status == "queued").count() == 0


def test_pool_respects_max_workers(sessions) -> None:  # type: ignore[no-untyped-def]
    class TrackingClient:
        """Wraps FakeClient and records peak in-flight calls."""

        def __init__(self, inner: FakeClient) -> None:
            self._inner = inner
            self._lock = threading.Lock()
            self.in_flight = 0
            self.max_in_flight = 0

        @property
        def name(self) -> str:
            return "tracking"

        def complete(self, *, system: str, user: str) -> LLMResponse:
            with self._lock:
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                time.sleep(0.02)
                return self._inner.complete(system=system, user=user)
            finally:
                with self._lock:
                    self.in_flight -= 1

    profile, resume = _env()
    ids = _queue(sessions, 4)
    tracker = TrackingClient(_client())
    counts = _run_pool(
        session_factory=sessions,
        posting_ids=ids,
        client=tracker,  # type: ignore[arg-type]
        fast_client=_client(),
        profile=profile,
        resume=resume,
        budget=TokenBudget(max_tokens=100_000),
        max_input_chars=3000,
        max_workers=2,
    )
    assert sum(counts.values()) == 4
    assert tracker.max_in_flight <= 2


def test_pool_skips_stale_items(sessions) -> None:  # type: ignore[no-untyped-def]
    profile, resume = _env()
    ids = _queue(sessions, 1)
    with sessions() as session:
        session.get(Posting, ids[0]).pipe_status = "done"  # type: ignore[union-attr]
        session.commit()
    counts = _run_pool(
        session_factory=sessions,
        posting_ids=[*ids, 999_999],
        client=_client(),
        fast_client=_client(),
        profile=profile,
        resume=resume,
        budget=TokenBudget(max_tokens=100_000),
        max_input_chars=3000,
        max_workers=2,
    )
    assert counts == {"skipped-stale": 2}


def test_pool_request_budget_stops_gracefully(sessions, capsys) -> None:  # type: ignore[no-untyped-def]
    """Request cap stops intake; the rest stay queued; the stop is reported."""
    profile, resume = _env()
    ids = _queue(sessions, 3)
    with sessions() as session:  # distinct links -> distinct match content
        for i, pid in enumerate(ids):
            post = session.get(Posting, pid)
            assert post is not None
            post.link = f"https://acme.example/j/{i}"
        session.commit()
    counts = _run_pool(
        session_factory=sessions,
        posting_ids=ids,
        client=_client(),
        fast_client=_client(),
        profile=profile,
        resume=resume,
        budget=TokenBudget(max_tokens=100_000, max_requests=1),
        max_input_chars=3000,
        max_workers=1,  # deterministic: strictly serial intake
    )
    assert counts == {"suggested": 1}
    assert "request budget" in capsys.readouterr().out
    with sessions() as session:
        statuses = [session.get(Posting, pid).pipe_status for pid in ids]  # type: ignore[union-attr]
        assert statuses[0] == "done"
        # The rest never finish: still queued, or left "analyzing" when the
        # worker picked them up just as the budget ran out (both are
        # retryable; the next run re-triages "analyzing" back to queued).
        assert all(s in ("queued", "analyzing") for s in statuses[1:])


def test_pool_budget_exhausted_stops_gracefully(sessions) -> None:  # type: ignore[no-untyped-def]
    profile, resume = _env()
    ids = _queue(sessions, 3)
    counts = _run_pool(
        session_factory=sessions,
        posting_ids=ids,
        client=_client(),
        fast_client=_client(),
        profile=profile,
        resume=resume,
        budget=TokenBudget(max_tokens=1),  # first charge already exceeds
        max_input_chars=3000,
        max_workers=1,  # deterministic: exactly one item picked up
    )
    assert counts == {}
    with sessions() as session:
        assert session.query(Suggestion).count() == 0
        for pid in ids:
            status = session.get(Posting, pid).pipe_status  # type: ignore[union-attr]
            assert status in ("queued", "analyzing")


class _FailingClient:
    """LLMClient that always fails (drives fallback/breaker tests)."""

    def __init__(self) -> None:
        self.attempts = 0

    @property
    def name(self) -> str:
        return "failing"

    def complete(self, *, system: str, user: str) -> LLMResponse:
        self.attempts += 1
        raise LLMError("boom")


def test_fallback_tries_providers_in_configured_order() -> None:
    secondary = FakeClient(default='{"ok": true}')
    client: LLMClient = FallbackClient(_FailingClient(), secondary)
    response = client.complete(system="s", user="u")
    assert response.text == '{"ok": true}'
    assert len(secondary.calls) == 1


def test_fallback_first_success_wins() -> None:
    secondary = FakeClient(default='{"ok": true}')
    primary = FakeClient(default='{"primary": true}')
    client: LLMClient = FallbackClient(primary, secondary)
    assert client.complete(system="s", user="u").text == '{"primary": true}'
    assert secondary.calls == []


def test_breaker_opens_and_closes() -> None:
    now = [1000.0]
    breaker = CircuitBreaker(_FailingClient(), threshold=2, cooldown_s=60.0, _clock=lambda: now[0])
    with pytest.raises(LLMError):
        breaker.complete(system="s", user="u")
    with pytest.raises(LLMError):
        breaker.complete(system="s", user="u")
    assert breaker.is_open
    before = breaker.inner.attempts  # type: ignore[union-attr]
    with pytest.raises(LLMError, match="circuit open"):
        breaker.complete(system="s", user="u")
    assert breaker.inner.attempts == before  # type: ignore[union-attr]
    now[0] += 61.0
    assert not breaker.is_open
    breaker.inner = FakeClient(default='{"recovered": true}')
    assert breaker.complete(system="s", user="u").text == '{"recovered": true}'


def test_budget_exhausted_propagates_not_swallowed(sessions) -> None:  # type: ignore[no-untyped-def]
    """BudgetExhausted must escape run_pipeline so the pool can stop intake."""
    from jobguard.agent import run_pipeline

    profile, resume = _env()
    ids = _queue(sessions, 1, link="")  # linkless: forces the classify LLM path
    with sessions() as session:
        post = session.get(Posting, ids[0])
        assert post is not None
        with pytest.raises(BudgetExhausted):
            run_pipeline(
                post,
                body="SDE Intern at Acme",
                sender="jobs-noreply@linkedin.com",
                client=_client(),
                profile=profile,
                resume=resume,
                session=session,
                budget=TokenBudget(max_tokens=1),
            )
