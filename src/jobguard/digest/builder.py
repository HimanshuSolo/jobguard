"""Digest builder: morning / evening digests + urgent items (Phase 5).

Sections per SPEC 8: Closing soon, New jobs, Best matches (with score
reason + tailoring notes), Flagged suspicious, Needs your approval.
Resend rule: an item already recorded in `digests_sent` is skipped unless its
deadline or status changed since (tokens encode both).

"New" means *not yet included in any sent digest*, not "arrived on today's
UTC date": the morning run at 07:00 IST used to miss everything synced the
evening before. Ranking uses the LLM score when a posting was analyzed and
the local heuristic score otherwise, so un-analyzed jobs still rank (the
LLM only reaches a bounded top-N per run). Only listed items are recorded
as sent; anything over the size cap rolls into the next digest.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy.orm import Session

from jobguard.store.models import DigestSent, PendingAction, Posting, Suggestion

if TYPE_CHECKING:
    from jobguard.resume.loader import Resume

DigestKind = Literal["morning", "evening", "urgent"]

# Pipeline outcomes that never belong in a digest (scams get their own section).
HIDDEN_STATUSES = frozenset({"filtered", "skipped_non_job", "quarantined", "expired", "violations"})


def posted_or_arrived(post: Posting) -> datetime:
    """Publish date when known, else when JobGuard first saw it (naive UTC)."""
    stamp = post.posted_at or post.arrived_at or datetime(1970, 1, 1)
    return stamp.replace(tzinfo=None) if stamp.tzinfo else stamp


def posted_label(post: Posting, now: datetime | None = None) -> str:
    """'posted today' / 'posted 3d ago' / 'seen 2d ago' (when date unknown)."""
    now = (now or datetime.now(UTC)).replace(tzinfo=None)
    days = max(0, (now - posted_or_arrived(post)).days)
    verb = "posted" if post.posted_at else "seen"
    return f"{verb} today" if days == 0 else f"{verb} {days}d ago"


def effective_score(post: Posting) -> float:
    """LLM score once analyzed, else the local heuristic (cheap) score."""
    if post.pipe_status == "done" or (post.score or 0.0) > 0:
        return float(post.score or 0.0)
    return float(post.cheap_score or 0.0)


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
    new_total: int = 0  # all new relevant jobs, before the size cap
    tokens: list[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_empty(self) -> bool:
        """True when every content section is empty (still worth sending?)."""
        return not (self.closing or self.new_today or self.best or self.flagged)


def _keyword_note(post: Posting, resume: Resume | None) -> str:
    """Deterministic resume advice: JD tech terms missing from the resume."""
    if resume is None:
        return ""
    from jobguard.resume.checker import keyword_gap

    text = "\n".join([post.role or "", " ".join(post.skills or []), post.body or ""])
    matched, missing = keyword_gap(text, resume)
    parts: list[str] = []
    if missing:
        parts.append("add if true for you: " + ", ".join(missing[:6]))
    if matched:
        parts.append("put first: " + ", ".join(matched[:6]))
    return "; ".join(parts)


def _tailoring_note(session: Session, post: Posting, resume: Resume | None = None) -> str:
    suggestion = (
        session.query(Suggestion)
        .filter_by(posting_id=post.id)
        .order_by(Suggestion.id.desc())
        .first()
    )
    notes: list[str] = []
    if suggestion is not None:
        card: dict[str, Any] = suggestion.card or {}
        for item in card.get("suggestions", [])[:2]:
            if isinstance(item, dict) and item.get("text"):
                notes.append(str(item["text"])[:200])
    keywords = _keyword_note(post, resume)
    if keywords:
        notes.append("Keywords: " + keywords)
    return " | ".join(notes)


def build_digest(
    session: Session,
    *,
    kind: DigestKind,
    best_score: float = 75.0,
    urgent_hours: int = 48,
    max_best: int = 10,
    max_items: int = 60,
) -> Digest:
    """Assemble sections from the DB, honouring the no-resend rule."""
    now = datetime.now(UTC)
    seen = _sent_tokens(session)
    posts = session.query(Posting).order_by(Posting.id).all()

    digest = Digest(kind=kind, subject="")
    fresh = [
        p
        for p in posts
        if _token(p) not in seen and (p.pipe_status or "queued") not in HIDDEN_STATUSES
    ]

    def deadline_in(post: Posting) -> float | None:
        if post.deadline is None:
            return None
        aware = post.deadline if post.deadline.tzinfo else post.deadline.replace(tzinfo=UTC)
        return (aware - now).total_seconds() / 3600.0

    def is_scam(post: Posting) -> bool:
        return bool(post.scam_flag) or post.pipe_status == "flagged_scam"

    for post in fresh:
        hours = deadline_in(post)
        # Grace of one hour past the deadline: clock skew and just-closed
        # postings still deserve one showing; the no-resend token dedupes.
        if hours is not None and -1 <= hours <= urgent_hours and not is_scam(post):
            digest.closing.append(post)

    candidates = [p for p in fresh if not is_scam(p)]
    # New jobs: most recently posted first (unknown dates use first-seen).
    newest = sorted(
        candidates, key=lambda p: (posted_or_arrived(p), effective_score(p)), reverse=True
    )
    digest.new_total = len(newest)
    digest.new_today = newest[: max(max_items, 0)]
    ranked = sorted(
        candidates, key=lambda p: (effective_score(p), posted_or_arrived(p)), reverse=True
    )
    digest.best = [p for p in ranked if effective_score(p) >= best_score][:max_best]
    digest.flagged = [p for p in fresh if is_scam(p)]
    digest.pending_approvals = session.query(PendingAction).filter_by(status="pending").count()

    shown: dict[int, Posting] = {}
    for post in [*digest.closing, *digest.best, *digest.new_today, *digest.flagged]:
        shown.setdefault(id(post), post)
    digest.tokens = [_token(p) for p in shown.values()]

    parts = [kind, now.strftime("%Y-%m-%d")]
    if kind == "urgent":
        parts.append(f"{len(digest.closing) + len(digest.best)} urgent")
    else:
        parts.append(f"{digest.new_total} new, {len(digest.closing)} closing soon")
    digest.subject = "JobGuard " + " — ".join(parts)
    return digest


def _source_label(post: Posting) -> str:
    """Short 'where it came from' tag, e.g. naukri / board:razorpay."""
    seen = list(post.seen_on or []) or [post.source or ""]
    labels: list[str] = []
    for src in seen:
        parts = (src or "").split(":")
        label = parts[-1] if src.startswith("board:") else (parts[0] or "")
        if src in ("gmail", "imap"):
            from jobguard.sources.parsers import detect_platform

            platform = detect_platform(post.sender or "")
            label = platform if platform != "unknown" else "email"
        if label and label not in labels:
            labels.append(label)
    return ", ".join(labels[:3])


def _score_label(post: Posting) -> str:
    score = effective_score(post)
    kind = "AI" if post.pipe_status == "done" else "quick"
    return f"{score:.0f} ({kind})"


def _row(post: Posting, extra: str = "") -> str:
    title = html.escape(f"{post.role} at {post.company}".strip(" at"))
    where = html.escape(post.location or "location n/a")
    link = html.escape(post.link or "")
    anchor = f' <a href="{link}">apply</a>' if link else ""
    via = html.escape(_source_label(post))
    via_html = f" <small>via {via}</small>" if via else ""
    return f"<li><b>{title}</b> ({where}){anchor}{via_html}{extra}</li>"


def _short_gap(post: Posting, resume: Resume | None) -> str:
    if resume is None:
        return ""
    from jobguard.resume.checker import keyword_gap

    text = "\n".join([post.role or "", " ".join(post.skills or []), post.body or ""])
    _, missing = keyword_gap(text, resume)
    return ("missing: " + ", ".join(missing[:3])) if missing else ""


def _rest(digest: Digest) -> list[Posting]:
    best = {id(p) for p in digest.best} | {id(p) for p in digest.closing}
    return [p for p in digest.new_today if id(p) not in best]


def render_html(
    session: Session, digest: Digest, resume: Resume | None = None, *, personal: bool = True
) -> str:
    """Digest as HTML (all dynamic content escaped).

    ``personal=False`` (copies for DIGEST_RECIPIENTS) leaves out everything
    derived from the owner's resume and the owner's pending approvals.
    """
    if not personal:
        resume = None
    sections: list[str] = []
    if digest.best:
        chunks = []
        for post in digest.best:
            note = _tailoring_note(session, post, resume) if personal else ""
            extra = (
                f" — {html.escape(posted_label(post))} · score {html.escape(_score_label(post))}"
            )
            if note:
                extra += f"<br/>Resume: {html.escape(note)}"
            chunks.append(_row(post, extra))
        sections.append(f"<h2>Best matches</h2><ul>{''.join(chunks)}</ul>")
    if digest.closing:
        rows = []
        for post in digest.closing:
            label = post.deadline.isoformat() if post.deadline else "date unknown"
            rows.append(_row(post, f" — closes {html.escape(label)}"))
        sections.append(f"<h2>Closing soon</h2><ul>{''.join(rows)}</ul>")
    rest = _rest(digest)
    if rest:
        rows = []
        for post in rest:
            extra = f" — {html.escape(posted_label(post))} · {html.escape(_score_label(post))}"
            gap = _short_gap(post, resume)
            if gap:
                extra += f" — <small>{html.escape(gap)}</small>"
            rows.append(_row(post, extra))
        sections.append(
            f"<h2>New internships ({digest.new_total}), newest first</h2><ul>{''.join(rows)}</ul>"
        )
    hidden = digest.new_total - len(digest.new_today)
    if hidden > 0:
        sections.append(
            f"<p>+{hidden} more older postings: they will appear in the next digest "
            "and are on the dashboard now.</p>"
        )
    if digest.flagged:
        sections.append(
            f"<h2>Flagged suspicious</h2><ul>{''.join(_row(p) for p in digest.flagged)}</ul>"
        )
    if digest.pending_approvals and personal:
        sections.append(
            f"<h2>Needs your approval</h2><p>{digest.pending_approvals} pending action(s).</p>"
        )
    if digest.is_empty:
        sections.append("<p>No new postings since the last digest.</p>")
    return (
        f"<html><body><h1>{html.escape(digest.subject)}</h1>" + "".join(sections) + "</body></html>"
    )


def render_text(
    session: Session, digest: Digest, resume: Resume | None = None, *, personal: bool = True
) -> str:
    """Digest as plain text (same sections, no markup; see ``render_html``)."""
    if not personal:
        resume = None
    lines = [digest.subject, "=" * len(digest.subject), ""]

    def entry(post: Posting, extra: str = "") -> None:
        via = _source_label(post)
        lines.append(
            f"- {post.role} at {post.company} ({post.location or 'location n/a'})"
            f"{' via ' + via if via else ''} {extra}".rstrip()
        )
        if post.link:
            lines.append(f"  {post.link}")

    if digest.best:
        lines.append("Best matches:")
        for post in digest.best:
            entry(post, f"[{posted_label(post)}, score {_score_label(post)}]")
            note = _tailoring_note(session, post, resume) if personal else ""
            if note:
                lines.append(f"  Resume: {note}")
        lines.append("")
    if digest.closing:
        lines.append("Closing soon:")
        for post in digest.closing:
            entry(post, "(deadline near)")
        lines.append("")
    rest = _rest(digest)
    if rest:
        lines.append(f"New internships ({digest.new_total}), newest first:")
        for post in rest:
            gap = _short_gap(post, resume)
            entry(
                post, f"[{posted_label(post)}, {_score_label(post)}]" + (f" {gap}" if gap else "")
            )
        lines.append("")
    hidden = digest.new_total - len(digest.new_today)
    if hidden > 0:
        lines.append(f"+{hidden} more older postings (next digest / dashboard).")
        lines.append("")
    if digest.flagged:
        lines.append("Flagged suspicious (do NOT apply without review):")
        for post in digest.flagged:
            lines.append(f"- {post.role} at {post.company}")
        lines.append("")
    if digest.pending_approvals and personal:
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
