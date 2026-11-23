"""Digest builder: morning / evening digests + urgent items (Phase 5).

Sections per SPEC 8: Closing soon, New today, Best matches (with score
reason + tailoring notes), Flagged suspicious, Needs your approval.
Resend rule: an item already recorded in `digests_sent` is skipped unless its
deadline or status changed since (tokens encode both).
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from jobguard.store.models import DigestSent, PendingAction, Posting, Suggestion

DigestKind = Literal["morning", "evening", "urgent"]


def _token(post: Posting) -> str:
    deadline = post.deadline.isoformat() if post.deadline else "-"
    return f"p{post.id}:{deadline}:{post.status}"


def _sent_tokens(session: Session) -> set[str]:
    tokens: set[str] = set()
    for row in session.query(DigestSent).all():
        tokens.update(row.items or [])
    return tokens


@dataclass
class Digest:
    """One built digest, ready to render and send."""

    kind: str
    subject: str
    closing: list[Posting] = field(default_factory=list)
    new_today: list[Posting] = field(default_factory=list)
    best: list[Posting] = field(default_factory=list)
    flagged: list[Posting] = field(default_factory=list)
    pending_approvals: int = 0
    tokens: list[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_empty(self) -> bool:
        """True when every content section is empty (still worth sending?)."""
        return not (self.closing or self.new_today or self.best or self.flagged)


def _tailoring_note(session: Session, post: Posting) -> str:
    suggestion = (
        session.query(Suggestion)
        .filter_by(posting_id=post.id)
        .order_by(Suggestion.id.desc())
        .first()
    )
    if suggestion is None:
        return ""
    card: dict[str, Any] = suggestion.card or {}
    notes: list[str] = []
    for item in card.get("suggestions", [])[:2]:
        if isinstance(item, dict) and item.get("text"):
            notes.append(str(item["text"])[:200])
    return " | ".join(notes)


def build_digest(
    session: Session,
    *,
    kind: DigestKind,
    best_score: float = 75.0,
    urgent_hours: int = 48,
    max_best: int = 10,
) -> Digest:
    """Assemble sections from the DB, honouring the no-resend rule."""
    now = datetime.now(UTC)
    seen = _sent_tokens(session)
    posts = session.query(Posting).order_by(Posting.id).all()

    digest = Digest(kind=kind, subject="")
    fresh: list[Posting] = []
    for post in posts:
        token = _token(post)
        if token in seen:
            continue
        fresh.append(post)
        digest.tokens.append(token)

    def deadline_in(post: Posting) -> float | None:
        if post.deadline is None:
            return None
        aware = post.deadline if post.deadline.tzinfo else post.deadline.replace(tzinfo=UTC)
        return (aware - now).total_seconds() / 3600.0

    for post in fresh:
        hours = deadline_in(post)
        # Grace of one hour past the deadline: clock skew and just-closed
        # postings still deserve one showing; the no-resend token dedupes.
        if hours is not None and -1 <= hours <= urgent_hours and not post.scam_flag:
            digest.closing.append(post)

    today = now.date()
    digest.new_today = [
        p
        for p in fresh
        if not p.scam_flag and p.arrived_at is not None and p.arrived_at.date() == today
    ]

    digest.best = sorted(
        [p for p in fresh if not p.scam_flag and p.score >= best_score],
        key=lambda p: p.score,
        reverse=True,
    )[:max_best]

    digest.flagged = [p for p in fresh if p.scam_flag]
    digest.pending_approvals = session.query(PendingAction).filter_by(status="pending").count()

    parts = [kind, now.strftime("%Y-%m-%d")]
    if kind == "urgent":
        parts.append(f"{len(digest.closing) + len(digest.best)} urgent")
    else:
        parts.append(f"{len(digest.new_today)} new, {len(digest.closing)} closing soon")
    digest.subject = "JobGuard " + " — ".join(parts)
    return digest


def _row(post: Posting, extra: str = "") -> str:
    title = html.escape(f"{post.role} at {post.company}".strip(" at"))
    where = html.escape(post.location or "")
    link = html.escape(post.link or "")
    anchor = f' <a href="{link}">apply</a>' if link else ""
    return f"<li><b>{title}</b> ({where}){anchor}{extra}</li>"


def render_html(session: Session, digest: Digest) -> str:
    """Digest as HTML (all dynamic content escaped)."""
    sections: list[str] = []
    if digest.closing:
        rows = []
        for post in digest.closing:
            label = post.deadline.isoformat() if post.deadline else "date unknown"
            rows.append(_row(post, f" — closes {html.escape(label)}"))
        sections.append(f"<h2>Closing soon</h2><ul>{''.join(rows)}</ul>")
    if digest.new_today:
        sections.append(f"<h2>New today</h2><ul>{''.join(_row(p) for p in digest.new_today)}</ul>")
    if digest.best:
        chunks = []
        for post in digest.best:
            note = _tailoring_note(session, post)
            extra = f" — score {post.score:.0f}"
            if note:
                extra += f"<br/>Tailoring: {html.escape(note)}"
            chunks.append(_row(post, extra))
        sections.append(f"<h2>Best matches</h2><ul>{''.join(chunks)}</ul>")
    if digest.flagged:
        sections.append(
            f"<h2>Flagged suspicious</h2><ul>{''.join(_row(p) for p in digest.flagged)}</ul>"
        )
    if digest.pending_approvals:
        sections.append(
            f"<h2>Needs your approval</h2><p>{digest.pending_approvals} pending action(s).</p>"
        )
    if digest.is_empty:
        sections.append("<p>No new postings since the last digest.</p>")
    return (
        f"<html><body><h1>{html.escape(digest.subject)}</h1>" + "".join(sections) + "</body></html>"
    )


def render_text(session: Session, digest: Digest) -> str:
    """Digest as plain text (same sections, no markup)."""
    lines = [digest.subject, "=" * len(digest.subject), ""]

    def section(title: str, posts: list[Posting], extra: str = "") -> None:
        if not posts:
            return
        lines.append(f"{title}:")
        for post in posts:
            lines.append(f"- {post.role} at {post.company} ({post.location}) {extra}".rstrip())
            if post.link:
                lines.append(f"  {post.link}")
        lines.append("")

    section(
        "Closing soon",
        digest.closing,
        "(deadline near)",
    )
    section("New today", digest.new_today)
    section("Best matches", digest.best)
    if digest.flagged:
        lines.append("Flagged suspicious (do NOT apply without review):")
        for post in digest.flagged:
            lines.append(f"- {post.role} at {post.company}")
        lines.append("")
    if digest.pending_approvals:
        lines.append(f"Needs your approval: {digest.pending_approvals} pending action(s).")
        lines.append("")
    if digest.is_empty:
        lines.append("No new postings since the last digest.")
    return "\n".join(lines)


def record_sent(session: Session, digest_id: str, digest: Digest) -> DigestSent:
    """Persist the send so the no-resend rule holds across runs."""
    row = DigestSent(digest_id=digest_id, items=list(digest.tokens), sent_at=datetime.now(UTC))
    session.add(row)
    session.commit()
    return row
