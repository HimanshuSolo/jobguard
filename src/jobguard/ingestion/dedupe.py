"""Cross-platform dedupe: merge NormalizedPostings sharing a canonical_key.

Two passes. Pass 1 groups by the full ``canonical_key`` (company + role +
location + normalized URL), so tracking-parameter variants collapse. Pass 2
merges groups whose normalized (company, role, location) triple matches but
whose links differ (e.g. a LinkedIn redirect vs the company's own apply URL):
same job, different doors. The earliest link is kept; ``seen_on`` is unioned.
"""

from __future__ import annotations

from jobguard.ingestion.normalize import NormalizedPosting, key_of, normalize_text


def _union(first: list[str], second: list[str]) -> list[str]:
    merged = list(first)
    for value in second:
        if value not in merged:
            merged.append(value)
    return merged


def _first(*values: str) -> str:
    for value in values:
        if value.strip():
            return value
    return ""


def merge_pair(older: NormalizedPosting, newer: NormalizedPosting) -> NormalizedPosting:
    """Merge two duplicates; first non-empty scalar wins, lists union."""
    return NormalizedPosting(
        company=_first(older.company, newer.company),
        role=_first(older.role, newer.role),
        location=_first(older.location, newer.location),
        link=_first(older.link, newer.link),
        skills=_union(older.skills, newer.skills),
        eligibility=newer.eligibility
        if len(newer.eligibility) > len(older.eligibility)
        else older.eligibility,
        source=older.source or newer.source,
        sender=_first(older.sender, newer.sender),
        body=newer.body if len(newer.body) > len(older.body) else older.body,
        seen_on=_union(older.seen_on, newer.seen_on),
        published=_first(older.published, newer.published),
    )


def _triple(post: NormalizedPosting) -> tuple[str, str, str]:
    return (
        normalize_text(post.company),
        normalize_text(post.role),
        normalize_text(post.location),
    )


def _is_empty(post: NormalizedPosting) -> bool:
    return not any([post.company, post.role, post.location, post.link])


def merge_duplicates(posts: list[NormalizedPosting]) -> list[NormalizedPosting]:
    """Collapse duplicates by canonical_key, then by company/role/location.

    Fully empty postings never merge: they carry no identity, so collapsing
    them would silently drop distinct unknown items.
    """
    merged: dict[str, NormalizedPosting] = {}
    order: list[str] = []
    empties: list[NormalizedPosting] = []
    for post in posts:
        if _is_empty(post):
            empties.append(post)
            continue
        key = key_of(post)
        if key in merged:
            merged[key] = merge_pair(merged[key], post)
        else:
            merged[key] = post
            order.append(key)
    by_key = [merged[key] for key in order]
    # Pass 2: same triple, different links -> one posting.
    final: dict[tuple[str, str, str], NormalizedPosting] = {}
    final_order: list[tuple[str, str, str]] = []
    for post in by_key:
        triple = _triple(post)
        if triple in final and any(triple):
            final[triple] = merge_pair(final[triple], post)
        else:
            final[triple] = post
            final_order.append(triple)
    return [final[triple] for triple in final_order] + empties
