"""Tests for normalize / dedupe / prefilter / sync."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from jobguard.ingestion import (
    NormalizedPosting,
    Prefilter,
    canonical_key,
    keep,
    key_of,
    merge_duplicates,
    normalize_item,
    normalize_url,
)
from jobguard.sources.base import SourceItem


def _mail_item(
    company: str, role: str, location: str, link: str, source: str = "gmail"
) -> SourceItem:
    return SourceItem(
        source=source,
        external_id=link,
        payload={
            "sender": "jobs-noreply@linkedin.com",
            "parsed": {
                "company": company,
                "role": role,
                "location": location,
                "link": link,
            },
        },
    )


def test_normalize_url_strips_tracking() -> None:
    assert (
        normalize_url("https://Example.COM/jobs/1?utm_source=x&jk=abc#frag")
        == "https://example.com/jobs/1?jk=abc"
    )
    assert normalize_url("") == ""


def test_canonical_key_ignores_tracking_params() -> None:
    assert canonical_key(
        "Acme", "SDE Intern", "Remote", "https://x.example/1?utm_source=a"
    ) == canonical_key("Acme", "SDE Intern", "Remote", "https://x.example/1")


def test_cross_platform_merge() -> None:
    linked = normalize_item(
        _mail_item(
            "Acme Labs",
            "SDE Intern",
            "Remote",
            "https://www.linkedin.com/jobs/view/1?utm_source=alert",
        )
    )
    board = NormalizedPosting(
        company="acme labs",
        role="sde intern",
        location="remote",
        link="https://acme.example/careers/sde-intern",
        source="board:lever:acme",
        seen_on=["board:lever:acme"],
    )
    merged = merge_duplicates([linked, board])
    assert len(merged) == 1
    assert merged[0].link == linked.link  # earliest link kept
    assert sorted(merged[0].seen_on) == ["board:lever:acme", "gmail"]


def test_different_roles_do_not_merge() -> None:
    posts = [
        _mail_item("Acme", "SDE Intern", "Remote", "https://x.example/1"),
        _mail_item("Acme", "Recruiter", "Remote", "https://x.example/2"),
    ]
    assert len(merge_duplicates([normalize_item(p) for p in posts])) == 2


def test_empty_posts_do_not_merge() -> None:
    blanks = [NormalizedPosting(), NormalizedPosting()]
    assert len(merge_duplicates(blanks)) == 2


def test_prefilter_keywords() -> None:
    prefilter = Prefilter.from_settings("sde,software", "linkedin")
    post = normalize_item(_mail_item("Acme", "SDE Intern", "Remote", "https://x.example/1"))
    assert keep(post, "jobs-noreply@linkedin.com", prefilter) == (True, "matched")
    marketing = NormalizedPosting(company="Acme", role="Marketing Manager")
    kept, _ = keep(marketing, "jobs-noreply@linkedin.com", prefilter)
    assert kept is False


def test_prefilter_unknown_sender_gets_benefit_of_doubt() -> None:
    prefilter = Prefilter.from_settings("sde", "linkedin")
    post = normalize_item(
        _mail_item("Acme", "SDE Intern", "Remote", "https://x.example/1", source="gmail")
    )
    kept, _ = keep(post, "careers@acme.example", prefilter)
    assert kept is True


def test_prefilter_known_sender_not_listed_dropped() -> None:
    prefilter = Prefilter.from_settings("", "naukri")
    post = normalize_item(_mail_item("Acme", "SDE Intern", "Remote", "https://x.example/1"))
    kept, reason = keep(post, "jobs-noreply@linkedin.com", prefilter)
    assert kept is False
    assert "sender" in reason


def test_sync_is_idempotent() -> None:
    from jobguard.ingestion import sync_source
    from jobguard.store import init_db

    class FakeAdapter:
        @property
        def source_name(self) -> str:
            return "fake"

        def fetch(self, since: str) -> list[SourceItem]:
            return [
                _mail_item("Acme", "SDE Intern", "Remote", "https://x.example/1"),
                _mail_item("Acme", "Recruiter", "Remote", "https://x.example/2"),
            ]

        def cursor_after(self, items: list[SourceItem], since: str) -> str:
            return "cursor-1"

    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        first = sync_source(
            FakeAdapter(),  # type: ignore[arg-type]
            session,
            Prefilter.from_settings("sde", "linkedin"),
        )
        assert (first.new_postings, first.filtered_out) == (1, 1)
        second = sync_source(
            FakeAdapter(),  # type: ignore[arg-type]
            session,
            Prefilter.from_settings("sde", "linkedin"),
        )
        assert second.new_postings == 0
        assert second.skipped_seen == 2
        from jobguard.store import Posting

        assert session.query(Posting).count() == 1


def test_sync_fetch_failure_logged() -> None:
    from jobguard.ingestion import sync_source
    from jobguard.store import AuditLog, init_db

    class FailingAdapter:
        @property
        def source_name(self) -> str:
            return "broken"

        def fetch(self, since: str) -> list[SourceItem]:
            raise TimeoutError("boom")

        def cursor_after(self, items: list[SourceItem], since: str) -> str:
            return since

    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        result = sync_source(FailingAdapter(), session)  # type: ignore[arg-type]
        assert result.fetched == 0
        failures = session.query(AuditLog).filter_by(event="sync.fetch_failed").all()
        assert len(failures) == 1
        assert failures[0].detail["error"] == "TimeoutError"


def test_key_of_matches_canonical_key() -> None:
    post = NormalizedPosting(company="Acme", role="SDE", location="R", link="https://x.example/1")
    assert key_of(post) == canonical_key("Acme", "SDE", "R", "https://x.example/1")
