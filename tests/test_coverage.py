"""Coverage fixes: more jobs in, irrelevant jobs out, everything ranked.

Synthetic fixtures only. Each test pins one cause of the "stuck at ~15
results" symptom: digest emails collapsing to one job, duplicate keys
aborting a sync, cross-source duplicates never merging, non-India and
non-dev roles leaking in, and un-analyzed jobs never reaching the digest.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from jobguard.digest import build_digest, record_sent, render_text
from jobguard.ingestion import Prefilter, keep, normalize_item, sync_source
from jobguard.ingestion.normalize import NormalizedPosting
from jobguard.relevance import is_dev_role, location_ok
from jobguard.resume.loader import Resume, ResumeItem
from jobguard.sources.adzuna import AdzunaAdapter
from jobguard.sources.base import SourceItem
from jobguard.sources.digest_split import split_digest_jobs
from jobguard.sources.mail import GmailApiMailSource
from jobguard.store import Posting, init_db


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


# --- relevance gates ---------------------------------------------------------


def test_dev_role_gate_uses_whole_words_on_the_title() -> None:
    assert is_dev_role("SDE Intern")
    assert is_dev_role("Backend Developer (Node.js)")
    assert is_dev_role("ML Engineer")
    assert not is_dev_role("HTML Email Designer Coordinator")  # "ml" inside "html"
    assert not is_dev_role("International Relations Associate")  # "intern" inside
    assert not is_dev_role("Inside Sales Executive – IT / Software Sales")
    assert not is_dev_role("Corporate Strategy Intern")


def test_location_gate_keeps_india_and_india_compatible_remote() -> None:
    for ok in ("Bengaluru, Karnataka", "Gurgaon/Gurugram", "Remote", "", "Hybrid", "Pan India"):
        assert location_ok(ok)[0], ok
    for bad in ("New York", "Singapore", "Remote - US", "London, UK", "Lisbon, Portugal"):
        assert not location_ok(bad)[0], bad
    # Profile extras cover towns missing from the built-in list, but a
    # generic extra like "Remote" must not vouch for a foreign remote role.
    assert location_ok("Kozhikode", ["Kozhikode"])[0]
    assert not location_ok("Remote - US", ["Remote"])[0]


def test_prefilter_india_scope() -> None:
    prefilter = Prefilter.from_settings("sde,developer", "", location_scope="india")
    india = NormalizedPosting(company="A", role="SDE Intern", location="Pune")
    abroad = NormalizedPosting(company="A", role="SDE Intern", location="Berlin, Germany")
    assert keep(india, "", prefilter)[0]
    assert not keep(abroad, "", prefilter)[0]


# --- digest email splitting --------------------------------------------------

_NAUKRI = """
[| | | |
| | [React JS Developer](https://www.naukri.com/jd/job-listings-react-js-developer-acme-soft-noida-0-to-1-years-221126002502?src=ja&utm_source=x)
---
| Acme Soft| | [](https://www.ambitionbox.com/reviews/acme)| [3.7](https://www.ambitionbox.com/reviews/acme)
| | | 6 months duration
| | | Internship|
---|---|---](https://www.naukri.com/jd/job-listings-react-js-developer-acme-soft-noida-0-to-1-years-221126002502?src=ja&utm_source=x)
[| | | |
| | [Backend Intern (Go)](https://www.naukri.com/jd/job-listings-backend-intern-go-beta-labs-bengaluru-0-to-1-years-191126504393?src=ja)
---
| Beta Labs|
| | | Internship|
---|---|---](https://www.naukri.com/jd/job-listings-backend-intern-go-beta-labs-bengaluru-0-to-1-years-191126504393?src=ja)
[Unsubscribe](https://www.naukri.com/unsub)
"""

_GLASSDOOR = """
Job alert: Software Engineer
| | | | | [| |  |
---
Gamma Corp 3.1 ★
| Applied AI Engineer
---
| Noida
---
| ₹9L (Glassdoor Est.)| 8d
---|---](https://www.glassdoor.co.in/partner/jobListing.htm?pos=101&jobListingId=1010326708275&utm_source=jobalert)
| | | | | [| | | Delta Pvt Ltd
---|---
| Full Stack Developer
---
| Gurgaon
---
| Easy Apply
---|---
| 7d](https://www.glassdoor.co.in/partner/jobListing.htm?pos=102&jobListingId=1010326708276)
"""

_HIRIST = (
    "Hi, Jobs recommended for you\n"
    "|  [ |  --- ](https://postoffice.hirist.com/CL0/https:%2F%2Fwww.hirist.tech%2Fj%2F"
    "ai-engineer-python-1680581%3Fref=ep/1/abc=452) "
    "[ AI Engineer Epsilon Research  ·  1-2 Yrs  ·  Gurgaon/Gurugram ]"
    "(https://postoffice.hirist.com/CL0/https:%2F%2Fwww.hirist.tech%2Fj%2F"
    "ai-engineer-python-1680581%3Fref=ep/2/abc=452)\n"
    "[ Backend Developer Zeta Tech  ·  0-2 Yrs  ·  Bangalore ]"
    "(https://postoffice.hirist.com/CL0/https:%2F%2Fwww.hirist.tech%2Fj%2F"
    "backend-developer-1680685%3Fref=ep/2/def=452)\n"
)


def test_naukri_digest_splits_into_jobs() -> None:
    jobs = split_digest_jobs("Naukri <jobalert@naukri.com>", "Jobs for you", _NAUKRI)
    assert [(j.role, j.company, j.location) for j in jobs] == [
        ("React JS Developer", "Acme Soft", "Noida"),
        ("Backend Intern (Go)", "Beta Labs", "Bengaluru"),
    ]
    # Tracking query stripped; each job carries its own card as body.
    assert jobs[0].link.endswith("-221126002502")
    assert "Acme Soft" in jobs[0].snippet and "Beta Labs" not in jobs[0].snippet


def test_glassdoor_digest_splits_into_jobs() -> None:
    jobs = split_digest_jobs("Glassdoor <noreply@glassdoor.com>", "12 jobs", _GLASSDOOR)
    assert [(j.role, j.company, j.location) for j in jobs] == [
        ("Applied AI Engineer", "Gamma Corp", "Noida"),
        ("Full Stack Developer", "Delta Pvt Ltd", "Gurgaon"),
    ]


def test_hirist_digest_splits_and_unwraps_tracking() -> None:
    jobs = split_digest_jobs("hirist <info@hirist.com>", "10+ Jobs", _HIRIST)
    assert [j.location for j in jobs] == ["Gurgaon/Gurugram", "Bangalore"]
    assert jobs[0].role == "AI Engineer Epsilon Research"
    assert jobs[0].link == "https://www.hirist.tech/j/ai-engineer-python-1680581"


# --- Gmail search ------------------------------------------------------------


class _PagedGmail:
    def __init__(self, pages: list[list[str]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []
        self._kwargs: dict[str, Any] = {}

    def users(self) -> _PagedGmail:
        return self

    def messages(self) -> _PagedGmail:
        return self

    def list(self, **kwargs: Any) -> _PagedGmail:
        self._kwargs = kwargs
        self.calls.append(kwargs)
        return self

    def execute(self) -> dict[str, Any]:
        index = int(self._kwargs.get("pageToken") or 0)
        resp: dict[str, Any] = {"messages": [{"id": i} for i in self.pages[index]]}
        if index + 1 < len(self.pages):
            resp["nextPageToken"] = str(index + 1)
        return resp


def test_gmail_paginates_and_searches_job_mail_only() -> None:
    service = _PagedGmail([["a", "b"], ["c"]])
    source = GmailApiMailSource(service, lookback_days=5)
    assert source.list_ids("") == ["a", "b", "c"]
    query = service.calls[0]["q"]
    assert query.startswith("newer_than:5d ")
    assert "naukri.com" in query and "subject:(" in query


def test_gmail_respects_max_messages() -> None:
    source = GmailApiMailSource(_PagedGmail([["a", "b"], ["c"]]), max_messages=2)
    assert source.list_ids("") == ["a", "b"]


# --- sync: combining results -------------------------------------------------


class _ListAdapter:
    def __init__(self, name: str, items: list[SourceItem]) -> None:
        self._name = name
        self._items = items

    @property
    def source_name(self) -> str:
        return self._name

    def fetch(self, since: str) -> list[SourceItem]:
        return self._items

    def cursor_after(self, items: list[SourceItem], since: str) -> str:
        return "c"


def _board_item(ext: str, title: str, company: str, location: str, url: str) -> SourceItem:
    return SourceItem(
        source="board:greenhouse:x",
        external_id=ext,
        payload={"company": company, "title": title, "location": location, "url": url},
    )


def test_same_job_twice_in_one_batch_merges_instead_of_crashing() -> None:
    session = _session()
    item = _board_item("1", "SDE Intern", "Acme", "Pune", "https://acme.example/j/1")
    twin = _board_item("2", "SDE Intern", "Acme", "Pune", "https://acme.example/j/1")
    result = sync_source(_ListAdapter("boards", [item, twin]), session)
    assert (result.new_postings, result.merged_postings) == (1, 1)
    assert session.query(Posting).count() == 1


def test_same_job_from_two_sources_with_different_links_merges() -> None:
    session = _session()
    board = _board_item("1", "SDE Intern", "Acme", "Pune", "https://acme.example/j/1")
    sync_source(_ListAdapter("boards", [board]), session)
    mail = SourceItem(
        source="gmail",
        external_id="m1#0",
        payload={
            "sender": "jobs-noreply@linkedin.com",
            "parsed": {
                "company": "ACME",
                "role": "SDE  Intern",
                "location": "pune",
                "link": "https://www.linkedin.com/comm/jobs/view/42",
            },
        },
    )
    result = sync_source(_ListAdapter("gmail", [mail]), session)
    assert result.merged_postings == 1
    (row,) = session.query(Posting).all()
    assert set(row.seen_on) == {"board:greenhouse:x", "gmail"}
    assert row.link == "https://acme.example/j/1"  # earliest link kept


# --- Adzuna ------------------------------------------------------------------


def test_adzuna_maps_and_dedupes_across_queries() -> None:
    body = {
        "results": [
            {
                "id": 7,
                "title": "<strong>Software</strong> Engineer Intern",
                "company": {"display_name": "Acme"},
                "location": {"display_name": "Bengaluru, Karnataka"},
                "redirect_url": "https://www.adzuna.in/land/ad/7",
                "description": "Python <b>FastAPI</b>",
                "created": "2026-11-23T10:00:00Z",
            }
        ]
    }
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, content=json.dumps(body).encode())

    adapter = AdzunaAdapter(
        app_id="id",
        app_key="key",
        queries=["sde intern", "developer intern"],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolve=lambda host: ["93.184.216.34"],
    )
    items = adapter.fetch("")
    assert len(items) == 1 and len(seen_urls) == 2  # short page: no page 2
    post = normalize_item(items[0])
    assert (post.role, post.company, post.location) == (
        "Software Engineer Intern",
        "Acme",
        "Bengaluru, Karnataka",
    )
    assert "/jobs/in/search/1" in seen_urls[0]


# --- digest: every relevant new job, ranked ----------------------------------


def _post(session: Session, role: str, **kw: Any) -> Posting:
    post = Posting(
        company=kw.pop("company", "Acme"),
        role=role,
        location=kw.pop("location", "Pune"),
        canonical_key=f"k-{role}-{kw.get('cheap_score', 0)}",
        arrived_at=datetime(2026, 1, 1, tzinfo=UTC),  # long before "today"
        **kw,
    )
    session.add(post)
    session.commit()
    return post


def test_digest_ranks_unanalyzed_jobs_by_cheap_score_and_hides_filtered() -> None:
    session = _session()
    low = _post(session, "QA Intern", cheap_score=20.0, pipe_status="queued")
    high = _post(session, "Backend SDE Intern", cheap_score=80.0, pipe_status="queued")
    analyzed = _post(session, "Rust Developer", score=90.0, pipe_status="done")
    dropped = _post(session, "Sales Lead", cheap_score=99.0, pipe_status="filtered")
    newsletter = _post(session, "Weekly digest", pipe_status="skipped_non_job")

    digest = build_digest(session, kind="morning", best_score=75.0)
    assert digest.new_today == [analyzed, high, low]  # old arrivals still count
    assert digest.best == [analyzed, high]  # quick score can qualify too
    assert dropped not in digest.new_today and newsletter not in digest.new_today
    assert digest.new_total == 3


def test_digest_cap_rolls_overflow_into_next_digest() -> None:
    session = _session()
    for i in range(5):
        _post(session, f"SDE {i}", cheap_score=float(10 * i), pipe_status="queued")
    first = build_digest(session, kind="morning", best_score=101.0, max_items=3)
    assert len(first.new_today) == 3 and first.new_total == 5
    record_sent(session, "morning:1", first)
    second = build_digest(session, kind="morning", best_score=101.0, max_items=3)
    assert [p.role for p in second.new_today] == ["SDE 1", "SDE 0"]


def test_digest_text_has_resume_keywords_for_unanalyzed_jobs() -> None:
    session = _session()
    _post(
        session,
        "Backend SDE Intern",
        cheap_score=80.0,
        pipe_status="queued",
        body="We use Python, Kafka and Kubernetes.",
    )
    resume = Resume(
        owner="T",
        items=[ResumeItem(id="exp-1", title="Intern", detail="Built APIs in Python")],
    )
    digest = build_digest(session, kind="morning", best_score=75.0)
    text = render_text(session, digest, resume)
    assert "Keywords:" in text and "kafka" in text.casefold()
    assert "(quick)" in text


# --- ranking / keyword quality ----------------------------------------------


def test_level_words_alone_never_make_a_dev_role() -> None:
    # A configured list containing "intern" (old .env default) must not let
    # every "... Intern" title through.
    keywords = ["sde", "developer", "intern"]
    assert not is_dev_role("Product User Research, Intern", keywords=keywords)
    assert is_dev_role("Backend Developer Intern", keywords=keywords)


def test_new_grad_profile_ranks_senior_roles_down() -> None:
    from jobguard.agent.rank import Profile, score_posting

    profile = Profile(skills=["Python"], role_types=["Software Engineer"], new_grad=True)
    junior = score_posting(
        profile, role="Software Engineer Intern", location="", skills=[], text="Python"
    )
    senior = score_posting(
        profile, role="Staff Software Engineer", location="", skills=[], text="Python"
    )
    assert junior.score > senior.score + 30


def test_keyword_gap_keeps_phrases_whole_and_unique() -> None:
    from jobguard.resume.checker import keyword_gap

    resume = Resume(
        owner="T", items=[ResumeItem(id="e", title="ML", detail="deep learning, Python")]
    )
    matched, missing = keyword_gap(
        "Deep learning, Python, Azure, azure, New Relic, new team", resume
    )
    assert matched == ["python", "deep learning"]
    assert missing == ["azure", "new relic"]  # no "new", "deep", or duplicates


# --- extra digest recipients ---------------------------------------------------


def test_recipient_allowlist_is_config_only() -> None:
    from jobguard.notify.limits import check_sendable

    session = _session()
    kw = {"owner_email": "me@example.com", "max_per_day": 20}
    friends = ("friend@example.com",)
    assert check_sendable(session, to="Friend@Example.com", extra_recipients=friends, **kw)[0]
    assert not check_sendable(session, to="friend@example.com", **kw)[0]  # not configured
    assert not check_sendable(session, to="attacker@evil.example", extra_recipients=friends, **kw)[
        0
    ]


def test_shared_copy_has_no_resume_notes_or_approvals() -> None:
    from jobguard.digest import render_html
    from jobguard.store import PendingAction

    session = _session()
    _post(
        session,
        "Backend SDE Intern",
        cheap_score=80.0,
        pipe_status="queued",
        body="We use Python and Kafka.",
    )
    session.add(
        PendingAction(
            plan={"kind": "x"},
            risk="low",
            status="pending",
            token_hash="h",
            expires_at=datetime(2099, 1, 1),
        )
    )
    session.commit()
    resume = Resume(owner="T", items=[ResumeItem(id="e", title="Intern", detail="Python")])
    digest = build_digest(session, kind="morning", best_score=75.0)
    mine = render_text(session, digest, resume)
    shared = render_text(session, digest, resume, personal=False)
    assert "Keywords:" in mine and "Needs your approval" in mine
    assert "Backend SDE Intern" in shared
    assert "Keywords:" not in shared and "missing:" not in shared
    assert "approval" not in shared.casefold()
    assert "Resume:" not in render_html(session, digest, resume, personal=False)
