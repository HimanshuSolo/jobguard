"""DEMO_MODE seed data (Phase 6): synthetic postings, suggestions, approval.

Only ever synthetic fixtures; `seed-demo` refuses to run unless DEMO_MODE is
on, so real databases cannot be polluted. Idempotent: safe to re-run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from jobguard.policy import create_action
from jobguard.store.models import Posting, Suggestion


def seed_demo(session: Session) -> dict[str, int]:
    """Insert synthetic demo rows; returns counts per table."""
    now = datetime.now(UTC)
    existing = {p.canonical_key for p in session.query(Posting).all()}
    rows = [
        Posting(
            company="Acme Labs",
            role="SDE Intern",
            location="Remote",
            link="https://acme.example/careers/sde-intern",
            skills=["Python", "SQL"],
            eligibility="Final-year students; Python required.",
            source="board:lever:acme",
            sender="careers@acme.example",
            body="SDE Intern at Acme Labs. Python role.",
            seen_on=["board:lever:acme", "gmail"],
            canonical_key="demo-acme-sde",
            score=88.0,
        ),
        Posting(
            company="Beta Systems",
            role="Backend Developer Intern",
            location="Bengaluru",
            link="https://betasystems.example/jobs/7",
            skills=["FastAPI"],
            source="gmail",
            sender="jobs-noreply@linkedin.com",
            seen_on=["gmail"],
            canonical_key="demo-beta-backend",
            score=64.0,
        ),
        Posting(
            company="Gamma Soft",
            role="SDE Intern",
            location="Hybrid",
            link="https://gamma.example/apply/3",
            deadline=now + timedelta(hours=30),
            source="rss:careers",
            seen_on=["rss:careers"],
            canonical_key="demo-gamma-closing",
            score=81.0,
        ),
        Posting(
            company="Scam Corp",
            role="Data Entry",
            location="Work from home",
            link="https://scam-offer.example/apply",
            source="gmail",
            sender="hr.scam@gmail.com",
            seen_on=["gmail"],
            canonical_key="demo-scam",
            score=0.0,
            scam_flag=True,
        ),
    ]
    added = 0
    for row in rows:
        if row.canonical_key in existing:
            continue
        session.add(row)
        added += 1
    session.commit()

    acme = session.query(Posting).filter_by(canonical_key="demo-acme-sde").first()
    suggestions = 0
    if acme is not None and not session.query(Suggestion).filter_by(posting_id=acme.id).first():
        session.add(
            Suggestion(
                posting_id=acme.id,
                card={
                    "posting_key": "demo-acme-sde",
                    "suggestions": [
                        {
                            "kind": "reword",
                            "resume_item_ids": ["exp-1"],
                            "text": "Emphasize the FastAPI dashboard from exp-1.",
                        },
                        {
                            "kind": "gap",
                            "resume_item_ids": [],
                            "text": "Requires Go; not in resume.",
                        },
                    ],
                    "gaps": ["Go"],
                },
                resume_item_ids=["exp-1"],
                gaps=["Go"],
            )
        )
        suggestions = 1
    session.commit()
    pending = ensure_demo_approval(session)
    return {"postings": added, "suggestions": suggestions, "pending": pending}


def _pending_exists(session: Session) -> bool:
    from jobguard.store.models import PendingAction

    return session.query(PendingAction).filter_by(status="pending").count() > 0


def ensure_demo_approval(session: Session) -> int:
    """Create one demo pending action if none exists; returns 1/0."""
    if _pending_exists(session):
        return 0
    create_action(
        session,
        plan={"kind": "generate_tailored_resume", "posting_key": "demo-acme-sde"},
        risk="medium",
        expires_hours=72,
    )
    return 1
