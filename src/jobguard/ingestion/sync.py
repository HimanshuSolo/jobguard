"""Idempotent catch-up sync: adapter -> raw_postings -> postings.

Re-running a sync never duplicates work:
- ``emails_seen`` records every processed ``(source, external_id)`` pair,
- ``postings.canonical_key`` is unique, so re-fetched jobs merge instead of
  duplicating (``seen_on`` is unioned),
- the same job arriving from two sources with different links (LinkedIn
  redirect vs company board) merges on normalized (company, role, location),
- duplicates *within one batch* merge in memory before insert (they used to
  hit the unique constraint and abort the whole adapter's commit),
- ``run_state`` persists each adapter's cursor between runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from jobguard.ingestion.dedupe import merge_pair
from jobguard.ingestion.normalize import (
    NormalizedPosting,
    key_of,
    normalize_item,
    normalize_text,
    parse_when,
)
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


def _triple(post: NormalizedPosting) -> tuple[str, str, str] | None:
    """Cross-source identity; None when company or role is unknown."""
    company, role = normalize_text(post.company), normalize_text(post.role)
    if not company or not role:
        return None
    return (company, role, normalize_text(post.location))


def _index_existing(
    session: Session,
) -> tuple[dict[str, Posting], dict[tuple[str, str, str], Posting]]:
    by_key: dict[str, Posting] = {}
    by_triple: dict[tuple[str, str, str], Posting] = {}
    for row in session.query(Posting).all():
        by_key[row.canonical_key] = row
        triple = _triple(
            NormalizedPosting(
                company=row.company or "", role=row.role or "", location=row.location or ""
            )
        )
        if triple and triple not in by_triple:
            by_triple[triple] = row
    return by_key, by_triple


def _merge_into(existing: Posting, post: NormalizedPosting) -> None:
    """Fold ``post`` into a stored (or pending) row; the canonical key is kept."""
    merged = merge_pair(
        NormalizedPosting(
            company=existing.company or "",
            role=existing.role or "",
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
    _merge_posted(existing, post)


def _merge_posted(existing: Posting, post: NormalizedPosting) -> bool:
    """Keep the earliest known publish date; True when it changed."""
    posted = parse_when(post.published)
    if posted is None:
        return False
    if existing.posted_at is None or posted < existing.posted_at:
        existing.posted_at = posted
        return True
    return False


def _seen_keys(session: Session, keys: list[str]) -> set[str]:
    found: set[str] = set()
    unique = list(dict.fromkeys(keys))
    for start in range(0, len(unique), 500):
        chunk = unique[start : start + 500]
        rows = session.query(EmailSeen.message_id).filter(EmailSeen.message_id.in_(chunk))
        found.update(row[0] for row in rows)
    return found


def _place(
    post: NormalizedPosting,
    session: Session,
    by_key: dict[str, Posting],
    by_triple: dict[tuple[str, str, str], Posting],
    result: SyncResult,
) -> None:
    """Merge ``post`` into a known posting or insert it as a new one."""
    canonical = key_of(post)
    triple = _triple(post)
    existing = by_key.get(canonical) or (by_triple.get(triple) if triple else None)
    if existing is not None:
        _merge_into(existing, post)
        result.merged_postings += 1
        return
    row = Posting(
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
        canonical_key=canonical,
        posted_at=parse_when(post.published),
    )
    session.add(row)
    by_key[canonical] = row
    if triple:
        by_triple[triple] = row
    result.new_postings += 1


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

    by_key, by_triple = _index_existing(session)
    # One bulk lookup instead of a query per item: against a hosted Postgres
    # (~150 ms per round trip) the per-item query took ~30 min for 6k jobs.
    keys = [_seen_key(i.source, i.external_id) for i in items]
    seen = _seen_keys(session, keys)
    drop_reasons: dict[str, int] = {}
    with session.no_autoflush:
        for item, key in zip(items, keys, strict=True):
            if key in seen:
                result.skipped_seen += 1
                # Backfill publish dates on rows stored before dates were kept.
                old = normalize_item(item)
                old_triple = _triple(old)
                row = by_key.get(key_of(old)) or (by_triple.get(old_triple) if old_triple else None)
                if row is not None and row.posted_at is None:
                    _merge_posted(row, old)
                continue
            seen.add(key)  # duplicates inside one batch count once
            session.add(EmailSeen(message_id=key, sender=str(item.payload.get("sender", ""))))
            result.new_raw += 1
            post = normalize_item(item)
            verdict_keep, reason = keep(
                post, str(item.payload.get("sender", "")), prefilter or Prefilter()
            )
            if not verdict_keep:
                # Only the seen-marker is stored for dropped items: raw
                # payloads of thousands of irrelevant board jobs would bloat
                # a free-tier database for nothing.
                result.filtered_out += 1
                bucket = reason.split("(")[0].split("'")[0].strip()[:60]
                drop_reasons[bucket] = drop_reasons.get(bucket, 0) + 1
                continue
            session.add(
                RawPosting(
                    source=item.source,
                    external_id=item.external_id,
                    # Cap stored payload size; adapters keep bodies bounded already.
                    payload=json.dumps(item.payload, ensure_ascii=True)[:20000],
                )
            )
            _place(post, session, by_key, by_triple, result)
    if drop_reasons:
        session.add(
            AuditLog(
                event="sync.prefilter_drop",
                detail={"source": adapter.source_name, "reasons": drop_reasons},
            )
        )

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
                "errors": list(getattr(adapter, "errors", []) or [])[:10],
                "policy": decision.decision,
            },
        )
    )
    session.commit()
    return result
