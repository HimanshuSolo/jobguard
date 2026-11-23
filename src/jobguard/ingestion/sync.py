"""Idempotent catch-up sync: adapter -> raw_postings -> postings.

Re-running a sync never duplicates work:
- ``emails_seen`` records every processed ``(source, external_id)`` pair,
- ``postings.canonical_key`` is unique, so re-fetched jobs merge instead of
  duplicating (``seen_on`` is unioned),
- ``run_state`` persists each adapter's cursor between runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from jobguard.ingestion.dedupe import merge_pair
from jobguard.ingestion.normalize import NormalizedPosting, key_of, normalize_item
from jobguard.ingestion.prefilter import Prefilter, keep
from jobguard.policy import PolicyContext, evaluate
from jobguard.sources.base import SourceAdapter
from jobguard.store.models import AuditLog, EmailSeen, Posting, RawPosting, RunState


@dataclass
class SyncResult:
    """Counts for one adapter sync (all deterministic, all logged)."""

    source: str
    fetched: int = 0
    new_raw: int = 0
    new_postings: int = 0
    merged_postings: int = 0
    filtered_out: int = 0
    skipped_seen: int = 0


def _seen_key(source: str, external_id: str) -> str:
    return f"{source}:{external_id}"


def sync_source(
    adapter: SourceAdapter,
    session: Session,
    prefilter: Prefilter | None = None,
) -> SyncResult:
    """Fetch, dedupe, prefilter, and persist one adapter's new items."""
    result = SyncResult(source=adapter.source_name)
    state = session.get(RunState, adapter.source_name)
    since = state.cursor if state else ""
    try:
        items = adapter.fetch(since)
    except Exception as exc:
        # Fail-safe: log the failure type (never content) and keep other sources running.
        session.add(
            AuditLog(
                event="sync.fetch_failed",
                detail={"source": adapter.source_name, "error": type(exc).__name__},
            )
        )
        session.commit()
        return result
    result.fetched = len(items)

    new_posts: list[NormalizedPosting] = []
    for item in items:
        key = _seen_key(item.source, item.external_id)
        if session.query(EmailSeen).filter_by(message_id=key).first():
            result.skipped_seen += 1
            continue
        session.add(EmailSeen(message_id=key, sender=str(item.payload.get("sender", ""))))
        session.add(
            RawPosting(
                source=item.source,
                external_id=item.external_id,
                # Cap stored payload size; adapters keep bodies bounded already.
                payload=json.dumps(item.payload, ensure_ascii=True)[:20000],
            )
        )
        result.new_raw += 1
        post = normalize_item(item)
        verdict_keep, reason = keep(
            post, str(item.payload.get("sender", "")), prefilter or Prefilter()
        )
        if not verdict_keep:
            result.filtered_out += 1
            session.add(
                AuditLog(
                    event="sync.prefilter_drop",
                    detail={"source": item.source, "reason": reason},
                )
            )
            continue
        existing = session.query(Posting).filter_by(canonical_key=key_of(post)).first()
        if existing:
            merged = merge_pair(
                NormalizedPosting(
                    company=existing.company,
                    role=existing.role,
                    location=existing.location or "",
                    link=existing.link or "",
                    skills=list(existing.skills or []),
                    eligibility=existing.eligibility or "",
                    source=existing.source or "",
                    sender=existing.sender or "",
                    body=existing.body or "",
                    seen_on=list(existing.seen_on or []),
                ),
                post,
            )
            existing.company = merged.company
            existing.role = merged.role
            existing.location = merged.location
            existing.link = merged.link
            existing.skills = merged.skills
            existing.eligibility = merged.eligibility
            existing.sender = merged.sender
            existing.body = merged.body
            existing.seen_on = merged.seen_on
            result.merged_postings += 1
        else:
            new_posts.append(post)

    for post in new_posts:
        session.add(
            Posting(
                company=post.company,
                role=post.role,
                location=post.location,
                link=post.link,
                skills=post.skills,
                eligibility=post.eligibility,
                source=post.source,
                sender=post.sender,
                body=post.body,
                seen_on=post.seen_on,
                canonical_key=key_of(post),
            )
        )
        result.new_postings += 1

    new_cursor = adapter.cursor_after(items, since)
    if state is None:
        session.add(RunState(source=adapter.source_name, cursor=new_cursor))
    else:
        state.cursor = new_cursor

    decision = evaluate(PolicyContext(tool="write_db"))
    session.add(
        AuditLog(
            event="sync.source",
            detail={
                "source": adapter.source_name,
                "fetched": result.fetched,
                "new_raw": result.new_raw,
                "new_postings": result.new_postings,
                "merged": result.merged_postings,
                "filtered": result.filtered_out,
                "skipped_seen": result.skipped_seen,
                "policy": decision.decision,
            },
        )
    )
    session.commit()
    return result
