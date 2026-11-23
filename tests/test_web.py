"""Tests for the dashboard (TestClient, in-process ASGI — no network)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from jobguard.config import Settings
from jobguard.policy import create_action
from jobguard.store import init_db, session_factory
from jobguard.web import create_app, hash_password, seed_demo
from jobguard.web.auth import CSRF_COOKIE, SESSION_COOKIE


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    db = tmp_path / "dash.db"
    settings = Settings(
        database_url=f"sqlite:///{db}",
        dashboard_username="admin",
        dashboard_secret_key="test-secret-key",
        dashboard_password_hash=hash_password("pw123"),
    )
    engine = create_engine(settings.database_url)
    init_db(engine)
    with Session(engine) as session:
        seed_demo(session)
    app = create_app(settings)
    return TestClient(app, base_url="http://testserver", raise_server_exceptions=False)


def _login(test_client: TestClient) -> None:
    resp = test_client.post(
        "/login",
        data={"username": "admin", "password": "pw123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert SESSION_COOKIE in test_client.cookies


def test_login_required_redirects(client: TestClient) -> None:
    assert client.get("/board", follow_redirects=False).status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_wrong_password_rejected(client: TestClient) -> None:
    resp = client.post("/login", data={"username": "admin", "password": "nope"})
    assert resp.status_code == 200
    assert "Invalid credentials" in resp.text
    assert SESSION_COOKIE not in client.cookies


def test_board_lists_demo_postings(client: TestClient) -> None:
    _login(client)
    resp = client.get("/board")
    assert resp.status_code == 200
    assert "Acme Labs" in resp.text
    assert "Data Entry" not in resp.text  # internships only by default


def test_board_filter_hides_scam(client: TestClient) -> None:
    _login(client)
    resp = client.get("/board", params={"hide_scam": "1", "intern": "0"})
    assert "suspicious" not in resp.text
    resp = client.get("/board", params={"intern": "0"})
    assert "Data Entry" in resp.text  # opt out of the internship filter


def test_detail_shows_tailoring(client: TestClient) -> None:
    _login(client)
    board = client.get("/board")
    assert "/postings/1" in board.text
    detail = client.get("/postings/1")
    assert "Emphasize the FastAPI dashboard" in detail.text
    assert "Gaps: Go" in detail.text
    assert "Analyze now" in detail.text


def test_analyze_button_reports_outcome(client: TestClient) -> None:
    _login(client)
    csrf = client.cookies.get(CSRF_COOKIE, "")
    assert csrf
    resp = client.post("/postings/1/analyze", data={"csrf": csrf})
    assert resp.status_code == 200
    # Demo settings use the fake provider, so analysis is refused — the
    # wiring (auth + CSRF + outcome notice) is what this asserts.
    assert "Analyze: refused" in resp.text


def test_analyze_csrf_enforced(client: TestClient) -> None:
    _login(client)
    resp = client.post("/postings/1/analyze", data={"csrf": "wrong"})
    assert "CSRF check failed" in resp.text


def test_security_headers(client: TestClient) -> None:
    resp = client.get("/login")
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "unpkg.com" in resp.headers["Content-Security-Policy"]


def _csrf(test_client: TestClient) -> str:
    return str(test_client.cookies.get(CSRF_COOKIE, ""))


def _form_action(test_client: TestClient, path: str) -> str:
    import re

    page = test_client.get(path)
    assert page.status_code == 200
    match = re.search(r'action="(/approvals/[^"]+)"', page.text)
    assert match is not None
    return match.group(1)


def test_approval_token_flow(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import re

    # The approved demo plan renders a file; keep it inside tmp.
    monkeypatch.chdir(tmp_path)
    _login(client)
    listing = client.get("/approvals")
    assert "generate_tailored_resume" in listing.text
    match = re.search(r"/approvals/by-id/(\d+)", listing.text)
    assert match is not None
    action_id = match.group(1)
    action_path = _form_action(client, f"/approvals/by-id/{action_id}")
    assert action_path == f"/approvals/id:{action_id}"
    assert "Approve" in client.get(f"/approvals/by-id/{action_id}").text
    decided = client.post(
        action_path,
        data={"decision": "approve", "csrf": _csrf(client)},
    )
    assert "Approved action" in decided.text
    again = client.post(
        action_path,
        data={"decision": "approve", "csrf": _csrf(client)},
    )
    assert "Nothing to review" in again.text  # single-use


def test_approval_csrf_enforced(client: TestClient) -> None:
    _login(client)
    listing = client.get("/approvals")
    import re

    match = re.search(r"/approvals/by-id/(\d+)", listing.text)
    assert match is not None
    resp = client.post(
        _form_action(client, f"/approvals/by-id/{match.group(1)}"),
        data={"decision": "approve", "csrf": "bogus"},
    )
    assert "CSRF check failed" in resp.text


def test_bearer_token_full_email_flow(tmp_path: Path) -> None:
    db = tmp_path / "token.db"
    settings = Settings(
        database_url=f"sqlite:///{db}",
        dashboard_username="admin",
        dashboard_secret_key="tok-secret",
        dashboard_password_hash=hash_password("pw"),
    )
    engine = create_engine(settings.database_url)
    init_db(engine)
    sessions = session_factory(engine)
    with sessions() as session:
        _, token = create_action(session, plan={"kind": "noop"}, risk="low")
    app_client = TestClient(create_app(settings))
    page = app_client.get(f"/approvals/{token}")
    assert "Approve" in page.text
    # The read-only token GET hands the bearer a CSRF cookie for their POST.
    csrf = str(app_client.cookies.get(CSRF_COOKIE, ""))
    assert csrf
    done = app_client.post(f"/approvals/{token}", data={"decision": "approve", "csrf": csrf})
    assert "Approved action" in done.text
    # Single-use: the same link is dead afterwards.
    dead = app_client.get(f"/approvals/{token}")
    assert "Invalid link" in dead.text


def test_audit_and_metrics_pages(client: TestClient) -> None:
    _login(client)
    audit = client.get("/audit")
    assert audit.status_code == 200
    metrics = client.get("/metrics")
    assert metrics.status_code == 200


def _bare_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path}/bare.db",
        dashboard_username="admin",
        dashboard_secret_key="bare-secret",
        dashboard_password_hash=hash_password("pw"),
        gmail_client_secret=str(tmp_path / "nope.json"),
        boards_config_path=str(tmp_path / "nope.yaml"),
        imap_host="",
        rss_feeds="",
        resume_path=str(tmp_path / "resume.yaml"),
    )


def test_sources_page_and_sync_now(tmp_path: Path) -> None:
    from sqlalchemy import create_engine

    settings = _bare_settings(tmp_path)
    engine = create_engine(settings.database_url)
    init_db(engine)
    test_client = TestClient(create_app(settings))
    test_client.post(
        "/login",
        data={"username": "admin", "password": "pw"},
    )
    resp = test_client.get("/sources")
    assert resp.status_code == 200
    assert "Sync now" in resp.text
    synced = test_client.post("/sync", data={"csrf": str(test_client.cookies.get(CSRF_COOKIE, ""))})
    assert "Gmail skipped" in synced.text  # nothing configured, no network touched


def test_resume_upload_roundtrip(tmp_path: Path) -> None:
    from sqlalchemy import create_engine

    settings = _bare_settings(tmp_path)
    engine = create_engine(settings.database_url)
    init_db(engine)
    test_client = TestClient(create_app(settings))
    test_client.post("/login", data={"username": "admin", "password": "pw"})
    page = test_client.get("/resume")
    assert "citable items" in page.text
    saved = test_client.post(
        "/resume",
        data={
            "content": "Skills: Python, SQL\n- Built an API with FastAPI\n- Wrote pytest suites",
            "csrf": str(test_client.cookies.get(CSRF_COOKIE, "")),
        },
    )
    assert "Resume saved" in saved.text
    assert "skill-u1" in saved.text  # parsed skill IDs render
    bad = test_client.post(
        "/resume",
        data={"content": "   ", "csrf": str(test_client.cookies.get(CSRF_COOKIE, ""))},
    )
    assert "nothing saved" in bad.text


def test_keyword_gap_unit() -> None:
    from jobguard.resume import Resume, ResumeItem, keyword_gap

    resume = Resume(
        owner="T",
        items=[
            ResumeItem(id="skill-1", title="Python"),
            ResumeItem(id="exp-1", title="Intern", detail="Built API with FastAPI"),
        ],
    )
    matched, missing = keyword_gap("SDE Intern. Python and Go required. Docker a plus.", resume)
    assert "python" in matched
    assert "go" in missing


def test_compact_resume_cached_and_capped() -> None:
    from jobguard.resume import (
        Resume,
        ResumeItem,
        clear_compact_cache,
        compact_resume,
    )

    clear_compact_cache()
    resume = Resume(
        owner="T",
        items=[
            ResumeItem(id="exp-1", title="Intern", detail="Built things with FastAPI daily"),
            ResumeItem(id="skill-1", title="Python"),
        ],
    )
    first = compact_resume(resume)
    assert "exp-1" in first and "skill-1" in first
    assert compact_resume(resume) == first  # content-keyed cache hit
    tiny = compact_resume(resume, max_chars=20)
    assert len(tiny) <= 20 + len("\n…[truncated]")
    clear_compact_cache()


def test_pipeline_input_cap_applies() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from jobguard.agent import TokenBudget, run_pipeline
    from jobguard.llm.fake import FakeClient
    from jobguard.store import Posting, init_db

    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    client = FakeClient(
        rules=[("[classify]", '{"kind": "posting", "confidence": 0.9}')],
        default="NOT-JSON",
    )
    with Session(engine) as session:
        post = Posting(company="Acme", role="SDE Intern", canonical_key="cap1")
        session.add(post)
        session.commit()
        from jobguard.agent import Profile
        from jobguard.resume import Resume

        run_pipeline(
            post,
            body="x" * 5000,
            sender="s",
            client=client,
            profile=Profile(),
            resume=Resume(owner="T"),
            session=session,
            budget=TokenBudget(max_tokens=100000),
            max_input_chars=100,
        )
        sent = client.calls[0]["user"]
        assert len(sent) <= 100 + len("[classify]\n…[input truncated]") + 50


def test_resume_dict_from_text_shapes() -> None:
    import tempfile
    from pathlib import Path as _Path

    from jobguard.resume import load_resume, resume_dict_from_text, write_resume_yaml

    yaml_text = "owner: T\nskills:\n  - id: s1\n    name: Python\n"
    data = resume_dict_from_text(yaml_text)
    assert data["skills"][0]["id"] == "s1"
    plain = resume_dict_from_text("Skills: Python, SQL\n- Built an API\n- Wrote tests")
    with tempfile.TemporaryDirectory() as tmp:
        target = str(_Path(tmp) / "resume.yaml")
        write_resume_yaml(plain, target)
        loaded = load_resume(target)  # round-trips through the real loader
        assert "skill-u1" in loaded.known_ids()
        assert "exp-u1" in loaded.known_ids()


def test_tailored_executor_writes_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jobguard.web.app import _execute_plan

    monkeypatch.chdir(tmp_path)
    out = _execute_plan({"kind": "generate_tailored_resume", "posting_key": "demo-x"})
    assert "Wrote data/private/tailored-demo-x.md" in out
    assert (tmp_path / "data" / "private" / "tailored-demo-x.md").exists()
    assert "No executor" in _execute_plan({"kind": "mystery"})
