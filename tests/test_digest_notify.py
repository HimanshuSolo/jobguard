"""Tests for digest builder, send policy, outbound scan, and approvals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from jobguard.digest import build_digest, record_sent, render_html, render_text
from jobguard.notify import (
    SendFailed,
    SendRefused,
    SmtpNotifier,
    WhatsAppNotifier,
    check_sendable,
    scan_outbound,
)
from jobguard.policy import ApprovalError, create_action, decide, verify_token
from jobguard.store import Posting, Suggestion, init_db


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    return Session(engine)


def _post(session: Session, **kwargs: object) -> Posting:
    from jobguard.ingestion import canonical_key

    company = str(kwargs.pop("company", "Acme"))
    role = str(kwargs.pop("role", "SDE Intern"))
    row = Posting(
        company=company,
        role=role,
        canonical_key=canonical_key(company, role, "", ""),
        **kwargs,  # type: ignore[arg-type]
    )
    session.add(row)
    session.commit()
    return row


def test_digest_sections() -> None:
    session = _db()
    soon = _post(
        session,
        deadline=datetime.now(UTC) + timedelta(hours=10),
        score=90.0,
        link="https://acme.example/j",
    )
    session.add(
        Suggestion(
            posting_id=soon.id,
            card={"suggestions": [{"text": "Emphasize X"}]},
            resume_item_ids=["exp-1"],
            gaps=[],
        )
    )
    old = _post(session, company="Old", deadline=datetime.now(UTC) - timedelta(days=1))
    scam = _post(session, company="Scam", role="X", scam_flag=True)
    digest = build_digest(session, kind="morning", best_score=75.0, urgent_hours=48)
    assert soon in digest.closing
    assert old not in digest.closing  # past deadline, not urgent
    assert soon in digest.best
    assert scam in digest.flagged
    assert scam not in digest.best  # flagged items are never recommended
    assert soon in digest.new_today  # arrived today
    html_body = render_html(session, digest)
    assert "Closing soon" in html_body and "Best matches" in html_body
    assert "Flagged suspicious" in html_body
    text = render_text(session, digest)
    assert "acme.example/j" in text


def test_no_resend_unless_changed() -> None:
    session = _db()
    post = _post(session, deadline=datetime.now(UTC) + timedelta(hours=10), score=90.0)
    first = build_digest(session, kind="morning")
    assert post in first.best
    record_sent(session, "morning:2026-11-23", first)
    second = build_digest(session, kind="evening")
    assert post not in second.best and post not in second.closing
    assert second.is_empty
    # Deadline change -> new token -> sent again.
    post.deadline = datetime.now(UTC) + timedelta(hours=5)
    session.commit()
    third = build_digest(session, kind="evening")
    assert post in third.closing


def test_deadline_now_counts_as_closing() -> None:
    session = _db()
    post = _post(session, deadline=datetime.now(UTC), score=10.0)
    digest = build_digest(session, kind="morning", best_score=75.0, urgent_hours=48)
    assert post in digest.closing


def test_tailoring_note_in_html() -> None:
    session = _db()
    post = _post(session, score=80.0)
    session.add(
        Suggestion(
            posting_id=post.id,
            card={"suggestions": [{"text": "Note text"}]},
            resume_item_ids=[],
            gaps=[],
        )
    )
    digest = build_digest(session, kind="morning", best_score=75.0)
    assert "Note text" in render_html(session, digest)


def test_send_policy_recipient_lock() -> None:
    session = _db()
    allowed, _ = check_sendable(
        session, to="owner@example.com", owner_email="owner@example.com", max_per_day=20
    )
    assert allowed is True
    allowed, reason = check_sendable(
        session, to="attacker@evil.example", owner_email="owner@example.com", max_per_day=20
    )
    assert allowed is False
    assert "OWNER_EMAIL" in reason


def test_send_policy_daily_cap() -> None:
    from jobguard.store import DigestSent

    session = _db()
    session.add(DigestSent(digest_id="m:x", items=[], sent_at=datetime.now(UTC)))
    session.commit()
    allowed, reason = check_sendable(
        session, to="owner@example.com", owner_email="owner@example.com", max_per_day=1
    )
    assert allowed is False
    assert "cap" in reason


def test_outbound_scan_blocks_secrets() -> None:
    assert scan_outbound("Hi", "body with sk-abcdefgh12345678 inside") == ["api-key"]
    assert scan_outbound("Hi", "-----BEGIN RSA PRIVATE KEY-----\n...") == ["private-key"]
    assert scan_outbound("digest", "SDE Intern at Acme, apply soon") == []


class FakeSmtp:
    instances: list[FakeSmtp] = []

    def __init__(self, *args: object) -> None:
        self.sent: list[object] = []
        FakeSmtp.instances.append(self)

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def starttls(self) -> None: ...
    def login(self, *args: object) -> None: ...
    def send_message(self, message: object) -> None:
        self.sent.append(message)


def _notifier(**kwargs: object) -> SmtpNotifier:
    FakeSmtp.instances.clear()
    base: dict[str, object] = {
        "host": "smtp.example.com",
        "port": 587,
        "user": "u",
        "password": "p",
        "from_address": "bot@example.com",
        "owner_email": "owner@example.com",
        "max_per_day": 20,
        "smtp_factory": FakeSmtp,
    }
    base.update(kwargs)
    return SmtpNotifier(**base)  # type: ignore[arg-type]


def test_smtp_send_ok() -> None:
    session = _db()
    _notifier().send(session, to="owner@example.com", subject="Hi", html="<p>Hi</p>", text="Hi")
    assert len(FakeSmtp.instances) == 1
    assert len(FakeSmtp.instances[0].sent) == 1


def test_smtp_wrong_recipient_refused() -> None:
    session = _db()
    with pytest.raises(SendRefused):
        _notifier().send(
            session, to="attacker@evil.example", subject="Hi", html="<p>Hi</p>", text="Hi"
        )
    assert FakeSmtp.instances == []


def test_smtp_secret_content_refused() -> None:
    session = _db()
    with pytest.raises(SendRefused, match="blocked"):
        _notifier().send(
            session,
            to="owner@example.com",
            subject="Hi",
            html="<p>sk-abcdefgh12345678</p>",
            text="x",
        )
    assert FakeSmtp.instances == []


def test_smtp_transport_failure() -> None:
    import smtplib

    class BoomSmtp(FakeSmtp):
        def send_message(self, message: object) -> None:
            raise smtplib.SMTPException("down")

    session = _db()
    with pytest.raises(SendFailed):
        _notifier(smtp_factory=BoomSmtp).send(
            session, to="owner@example.com", subject="Hi", html="<p>Hi</p>", text="Hi"
        )


def test_whatsapp_stub_raises() -> None:
    with pytest.raises(NotImplementedError):
        WhatsAppNotifier().send(to="x", subject="s", html="h", text="t")


def test_approval_happy_path() -> None:
    session = _db()
    action, token = create_action(session, plan={"kind": "tailor", "id": 1}, risk="high")
    found = verify_token(session, token)
    assert found is not None and found.id == action.id
    assert verify_token(session, "bogus-token") is None
    out = decide(session, action, approved=True, executor=lambda plan: f"did:{plan['kind']}")
    assert out == "did:tailor"
    assert verify_token(session, token) is None  # single-use: decided actions stop verifying
    with pytest.raises(ApprovalError):
        decide(session, action, approved=True)


def test_approval_reject_and_expiry() -> None:
    session = _db()
    action, token = create_action(session, plan={}, risk="high", expires_hours=-1)
    assert verify_token(session, token) is None  # already expired
    with pytest.raises(ApprovalError):
        decide(session, action, approved=True)
    action2, token2 = create_action(session, plan={}, risk="medium")
    decide(session, action2, approved=False)
    assert verify_token(session, token2) is None
