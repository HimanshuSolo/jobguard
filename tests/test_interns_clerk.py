"""Interns-only + 2027-batch gates, posted-date ordering, dashboard fixes, Clerk auth.

Clerk tests sign real RS256 JWTs with a throwaway key and inject it, so no
network and no real Clerk instance is touched.
"""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobguard.config import Settings
from jobguard.digest import build_digest
from jobguard.ingestion import Prefilter, keep, normalize_item, sync_source
from jobguard.ingestion.normalize import NormalizedPosting, parse_when
from jobguard.sources.base import SourceItem
from jobguard.sources.digest_split import split_digest_jobs
from jobguard.store import Posting, init_db
from jobguard.web.app import _safe_url, create_app
from jobguard.web.clerk import ClerkAuth, ClerkConfig, ClerkConfigError, frontend_api_from_key

FAPI = "example-app-12.clerk.accounts.dev"
PK = "pk_test_" + base64.b64encode(f"{FAPI}$".encode()).decode().rstrip("=")


# --- interns only, 2027 batch --------------------------------------------------


def _prefilter() -> Prefilter:
    return Prefilter.from_settings(
        "sde,software,developer,engineer,intern",
        "",
        location_scope="india",
        role_level="intern",
        grad_year=2027,
    )


@pytest.mark.parametrize(
    ("role", "body", "kept"),
    [
        ("SDE Intern", "", True),
        ("Software Engineer Intern - Backend", "Open to 2027 batch", True),
        ("SDE 3", "", False),
        ("Software Engineer II", "", False),
        ("Senior Software Engineer", "", False),
        ("Software Engineer", "", False),  # full-time, not an internship
        ("SDE Intern", "Eligibility: 2025 and 2026 batch only", False),
        ("SDE Intern", "Students graduating in 2026 or 2027", True),
        ("Sales Intern", "", False),  # internship, but not a dev role
    ],
)
def test_intern_and_batch_gates(role: str, body: str, kept: bool) -> None:
    post = NormalizedPosting(company="Acme", role=role, location="Pune", body=body)
    assert keep(post, "", _prefilter())[0] is kept


def test_retriage_filters_previously_analyzed_senior_roles(tmp_path: Path) -> None:
    from jobguard.worker import triage_stage

    url = f"sqlite:///{tmp_path}/t.db"
    engine = create_engine(url)
    init_db(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as session:
        session.add_all(
            [
                Posting(company="A", role="SDE 3", canonical_key="a", pipe_status="done", score=90),
                Posting(company="B", role="SDE Intern", canonical_key="b", location="Pune"),
            ]
        )
        session.commit()
    settings = Settings(database_url=url, profile_path=str(tmp_path / "none.yaml"))
    triage_stage(sessions, settings)
    with sessions() as session:
        status = {p.role: p.pipe_status for p in session.query(Posting).all()}
    assert status == {"SDE 3": "filtered", "SDE Intern": "queued"}


# --- posted dates -----------------------------------------------------------------


def test_parse_when_normalizes_to_naive_utc() -> None:
    assert parse_when("2026-09-14T03:49:28-04:00") == datetime(2026, 9, 14, 7, 49, 28)
    assert parse_when("2026-11-23T10:00:00Z") == datetime(2026, 11, 23, 10, 0, 0)
    assert parse_when("not a date") is None and parse_when("") is None


def test_glassdoor_age_dates_the_job_before_the_email() -> None:
    body = (
        "| | [| |\n---\nGamma Corp 3.1 ★\n| Backend Intern\n---\n| Noida\n---\n| 8d\n"
        "---|---](https://www.glassdoor.co.in/partner/jobListing.htm?jobListingId=1010)"
    )
    (job,) = split_digest_jobs("noreply@glassdoor.com", "Jobs", body)
    assert job.age_days == 8
    item = SourceItem(
        source="gmail",
        external_id="m#0",
        payload={"received_at": "2026-11-20T06:00:00+00:00", "parsed": job.model_dump()},
    )
    assert normalize_item(item).published.startswith("2026-11-12")


def test_sync_stores_and_backfills_posted_at() -> None:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()

    class Boards:
        source_name = "boards"

        def __init__(self, published: str) -> None:
            self.published = published

        def fetch(self, since: str) -> list[SourceItem]:
            return [
                SourceItem(
                    source="board:lever:x",
                    external_id="1",
                    payload={
                        "company": "Acme",
                        "title": "SDE Intern",
                        "location": "Pune",
                        "url": "https://acme.example/1",
                        "published": self.published,
                    },
                )
            ]

        def cursor_after(self, items: list[SourceItem], since: str) -> str:
            return ""

    sync_source(Boards(""), session)  # first seen without a date
    assert session.query(Posting).one().posted_at is None
    sync_source(Boards("2026-11-01T00:00:00Z"), session)  # already seen: backfill
    session.commit()
    assert session.query(Posting).one().posted_at == datetime(2026, 11, 1)


def test_digest_lists_newest_posted_first() -> None:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    now = datetime.now(UTC).replace(tzinfo=None)
    old = Posting(
        company="A",
        role="Old Intern",
        canonical_key="o",
        cheap_score=90,
        posted_at=now - timedelta(days=20),
    )
    new = Posting(
        company="B",
        role="New Intern",
        canonical_key="n",
        cheap_score=10,
        posted_at=now - timedelta(days=1),
    )
    session.add_all([old, new])
    session.commit()
    digest = build_digest(session, kind="morning", best_score=75.0)
    assert [p.role for p in digest.new_today] == ["New Intern", "Old Intern"]
    assert [p.role for p in digest.best] == ["Old Intern"]  # best stays score-ranked


# --- dashboard fixes ----------------------------------------------------------------


def test_safe_url_blocks_script_links() -> None:
    assert _safe_url("javascript:alert(1)") == ""
    assert _safe_url(" JaVaScRiPt:alert(1)") == ""
    assert _safe_url("data:text/html,x") == ""
    assert _safe_url("https://acme.example/j") == "https://acme.example/j"


def _password_app(tmp_path: Path) -> TestClient:
    from jobguard.web.auth import hash_password

    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/w.db",
        dashboard_username="admin",
        dashboard_password_hash=hash_password("pw"),
        dashboard_secret_key="s" * 32,
        resume_path=str(tmp_path / "none.yaml"),
    )
    engine = create_engine(settings.database_url)
    init_db(engine)
    with sessionmaker(bind=engine)() as session:
        now = datetime.now(UTC).replace(tzinfo=None)
        for i, days in enumerate([5, 1, 30]):
            session.add(
                Posting(
                    company=f"C{i}",
                    role=f"Intern {days}d",
                    canonical_key=f"k{i}",
                    posted_at=now - timedelta(days=days),
                    link="javascript:alert(1)" if i == 0 else "https://x.example",
                )
            )
        session.commit()
    client = TestClient(create_app(settings))
    client.post("/login", data={"username": "admin", "password": "pw"})
    return client


def test_board_newest_first_and_status_action(tmp_path: Path) -> None:
    client = _password_app(tmp_path)
    text = client.get("/board").text
    assert text.index("Intern 1d") < text.index("Intern 5d") < text.index("Intern 30d")
    assert "posted 1d ago" in text
    assert "htmx" not in text  # full-page swap into <main> nested the header

    detail = client.get("/postings/1").text
    assert "javascript:" not in detail  # unsafe apply link dropped
    csrf = client.cookies.get("jg_csrf", "")
    resp = client.post(
        "/postings/1/status", data={"value": "applied", "csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "applied" in client.get("/board", params={"status": "applied"}).text


def test_csrf_cookie_is_minted_for_forms(tmp_path: Path) -> None:
    client = _password_app(tmp_path)
    client.cookies.delete("jg_csrf")
    page = client.get("/postings/1")
    token = client.cookies.get("jg_csrf", "")
    assert token and f'value="{token}"' in page.text


# --- Clerk ----------------------------------------------------------------------------


def test_frontend_api_from_publishable_key() -> None:
    assert frontend_api_from_key(PK) == FAPI
    with pytest.raises(ClerkConfigError):
        frontend_api_from_key("sk_test_nope")


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(key: rsa.RSAPrivateKey, **claims: Any) -> str:
    now = int(time.time())
    payload = {
        "iss": f"https://{FAPI}",
        "sub": "user_abc",
        "iat": now,
        "nbf": now,
        "exp": now + 60,
        "azp": "http://testserver",
        **claims,
    }
    return jwt.encode(payload, key, algorithm="RS256")


def _auth(key: rsa.RSAPrivateKey, emails: list[str], **cfg: Any) -> ClerkAuth:
    config = ClerkConfig(
        publishable_key=PK,
        frontend_api=FAPI,
        secret_key=cfg.get("secret_key", "sk_test_x"),
        allowed_emails=frozenset({"me@example.com"}),
        allowed_user_ids=frozenset(cfg.get("ids", ())),
        authorized_parties=frozenset(cfg.get("parties", ())),
    )
    return ClerkAuth(config, signing_key=lambda _t: key.public_key(), user_lookup=lambda _u: emails)


def test_clerk_allows_only_the_owner(rsa_key: rsa.RSAPrivateKey) -> None:
    assert _auth(rsa_key, ["ME@example.com"]).verify(_token(rsa_key)).status == "ok"
    other = _auth(rsa_key, ["stranger@example.com"]).verify(_token(rsa_key))
    assert other.status == "forbidden" and other.user_id == "user_abc"


def test_clerk_rejects_bad_tokens(rsa_key: rsa.RSAPrivateKey) -> None:
    auth = _auth(rsa_key, ["me@example.com"])
    assert auth.verify("").status == "signed_out"
    assert auth.verify(_token(rsa_key, exp=int(time.time()) - 60)).status == "signed_out"
    assert auth.verify(_token(rsa_key, iss="https://evil.example")).status == "signed_out"
    forged = jwt.encode(
        {"iss": f"https://{FAPI}", "sub": "user_abc", "iat": 1, "exp": 2**31},
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        algorithm="RS256",
    )
    assert auth.verify(forged).status == "signed_out"


def test_clerk_azp_and_user_id_allowlist(rsa_key: rsa.RSAPrivateKey) -> None:
    pinned = _auth(rsa_key, ["me@example.com"], parties=["https://jobs.example"])
    assert pinned.verify(_token(rsa_key)).status == "forbidden"
    by_id = _auth(rsa_key, [], ids=["user_abc"], secret_key="")
    assert by_id.verify(_token(rsa_key)).status == "ok"
    # No secret and no id allowlist: fail closed.
    assert (
        _auth(rsa_key, ["me@example.com"], secret_key="").verify(_token(rsa_key)).status
        == "forbidden"
    )


def test_clerk_app_gates_pages_and_accepts_session(
    tmp_path: Path, rsa_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/c.db",
        clerk_publishable_key=PK,
        clerk_allowed_user_ids="user_abc",
        resume_path=str(tmp_path / "none.yaml"),
    )
    monkeypatch.setattr(ClerkAuth, "_key_for", lambda self, token: rsa_key.public_key())
    client = TestClient(create_app(settings))

    gate = client.get("/board")
    assert gate.status_code == 401
    assert 'data-state="signed_out"' in gate.text and 'data-retry="/board"' in gate.text
    assert f"https://{FAPI}/npm/@clerk/clerk-js@6/" in gate.text  # v5 is refused as outdated
    assert f"https://{FAPI}/npm/@clerk/ui@1/" in gate.text  # Core 3 UI bundle
    assert "/static/style.css?v=" in gate.text  # cache-busted assets
    signup = client.get("/sign-up")
    assert signup.status_code == 200 and 'data-mode="sign-up"' in signup.text
    login = client.get("/login")
    assert login.status_code == 200 and 'data-retry="/board"' in login.text
    csp = gate.headers["Content-Security-Policy"]
    assert f"https://{FAPI}" in csp and "challenges.cloudflare.com" in csp

    assert client.get("/healthz").json() == {"status": "ok"}  # Render probe stays public
    client.cookies.set("__session", _token(rsa_key))
    assert client.get("/board").status_code == 200
    client.cookies.set("__session", _token(rsa_key, sub="user_other"))
    denied = client.get("/board")
    assert denied.status_code == 403 and "user_other" in denied.text


# --- company replies are not job postings --------------------------------------


@pytest.mark.parametrize(
    ("subject", "alert"),
    [
        ("Application Received for Software Engineer Intern", False),
        ("CV Reviewed: Apprentice, Quality Assurance Engineer", False),
        ("Thank you for applying to Acme", False),
        ("Invitation: Online Assessment for SDE Intern", False),
        ("Interview scheduled: SDE Intern at Acme", False),
        ("Update: your internship application is still pending", False),
        ("Your OTP is 1234", False),
        ("Backend Developer at Trakkia and 11 more jobs in Gurgaon", True),
        ("10+ Jobs Matching Your Profile", True),
        ("Software Engineer Intern - Interview Ready? 5 new jobs", True),
        ("We are hiring: Backend Intern", True),
    ],
)
def test_job_alert_mail_gate(subject: str, alert: bool) -> None:
    from jobguard.relevance import is_job_alert_mail

    assert is_job_alert_mail(subject)[0] is alert


def test_gmail_adapter_skips_company_replies() -> None:
    from jobguard.sources.mail import GmailAlertAdapter, MailMessage

    class Box:
        name = "gmail"

        def list_ids(self, since: str) -> list[str]:
            return ["reply", "alert"]

        def get_message(self, message_id: str) -> MailMessage:
            subject = (
                "Application received: SDE Intern" if message_id == "reply" else "SDE Intern jobs"
            )
            return MailMessage(
                message_id=message_id,
                sender="jobs-noreply@linkedin.com",
                subject=subject,
                body_text="Title: SDE Intern\nCompany: Acme\nhttps://acme.example/j",
            )

    adapter = GmailAlertAdapter(Box())
    items = adapter.fetch("")
    assert [i.external_id for i in items] == ["alert#0"]
    assert adapter.skipped_mail == 1


def test_one_bad_message_does_not_discard_the_gmail_sync() -> None:
    from jobguard.sources.mail import GmailAlertAdapter, MailMessage

    class Box:
        name = "gmail"

        def list_ids(self, since: str) -> list[str]:
            return ["bad", "good"]

        def get_message(self, message_id: str) -> MailMessage:
            if message_id == "bad":
                raise RuntimeError("HttpError 429")
            return MailMessage(
                message_id=message_id,
                sender="jobs-noreply@linkedin.com",
                subject="SDE Intern jobs",
                body_text="Title: SDE Intern\nCompany: Acme\nhttps://acme.example/j",
            )

    adapter = GmailAlertAdapter(Box())
    assert [i.external_id for i in adapter.fetch("")] == ["good#0"]
    assert adapter.errors == ["message bad: RuntimeError"]
