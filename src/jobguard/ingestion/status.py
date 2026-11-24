"""Honest pipeline progress reporting (Phase 8, Step 2.1).

Statuses live on the posting row itself (``pipe_status``), so every report
is exact — never inferred from audit rows, which can alias deleted rows
after a table reset. Terminal states are never re-processed by later runs.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from jobguard.store.models import Posting, Suggestion

TERMINAL_STATUSES = frozenset(
    {
        "done",
        "skipped_non_job",
        "quarantined",
        "flagged_scam",
        "expired",
        "violations",
        "filtered",  # failed the role / India-location relevance gates
    }
)

RETRYABLE_STATUSES = frozenset({"queued", "analyzing", "failed"})


def summarize(session: Session) -> dict[str, Any]:
    """Count postings by pipeline state. Never counts queued as scored."""
    total = session.query(func.count(Posting.id)).scalar() or 0
    by_status: dict[str, int] = {}
    for status, count in session.query(Posting.pipe_status, func.count()).group_by(
        Posting.pipe_status
    ):
        by_status[str(status)] = int(count)
    done = by_status.get("done", 0)
    expired = by_status.get("expired", 0)
    queued = sum(by_status.get(s, 0) for s in RETRYABLE_STATUSES)
    with_suggestions = session.query(func.count(Suggestion.id)).scalar() or 0
    return {
        "total": total,
        "scored": done,
        "skipped_by_reason": {s: by_status.get(s, 0) for s in sorted(TERMINAL_STATUSES - {"done"})},
        "queued": queued,
        "expired": expired,
        "eligible": done + queued,
        "suggestions": with_suggestions,
    }


def backfill_statuses(session: Session) -> dict[str, int]:
    """Repair rows whose status predates the state machine.

    Only live table state is consulted (audit history can alias deleted rows
    after a reset, so it would lie): a suggestion present means done, scam
    flag means flagged_scam, past deadline means expired — regardless of any
    stale or default value. NULL rows with none of those become queued.
    Rows that already carry a real terminal status are never overwritten.
    Returns counts per assigned status.
    """
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    done_ids = {row[0] for row in session.query(Suggestion.posting_id).all()}
    assigned: dict[str, int] = {}
    for post in session.query(Posting).all():
        current = post.pipe_status or "queued"
        if post.id in done_ids:
            status = "done"
        elif current not in ("queued", "analyzing", None):
            continue  # a real decision already stands; never overwrite it
        elif post.scam_flag:
            status = "flagged_scam"
        elif (
            post.deadline is not None
            and (post.deadline if post.deadline.tzinfo else post.deadline.replace(tzinfo=UTC)) < now
        ):
            status = "expired"
        else:
            status = "queued"
        if status != current or post.pipe_status is None:
            post.pipe_status = status
            post.pipe_stage = "done" if status == "done" else "none"
            assigned[status] = assigned.get(status, 0) + 1
    session.commit()
    return assigned
