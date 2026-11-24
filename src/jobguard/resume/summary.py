"""Compact resume summaries for prompts (Phase 8, Step 2.5).

The full resume YAML is never pasted into prompts. Instead the pipeline gets
one line per citable item (``id: title -- detail keywords``), built once and
cached by content hash, so repeated postings reuse it with zero rebuild cost.
IDs are preserved verbatim: the fabrication checker still sees every citation.
"""

from __future__ import annotations

import hashlib
import json

from jobguard.resume.loader import Resume

_summary_cache: dict[str, str] = {}


def _fingerprint(resume: Resume, max_chars: int) -> str:
    payload = json.dumps(
        [max_chars, [[i.id, i.title, i.detail] for i in resume.items]],
        ensure_ascii=True,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def compact_resume(resume: Resume, max_chars: int = 1500) -> str:
    """One citable line per resume item, cached by content hash and cap."""
    key = _fingerprint(resume, max_chars)
    cached = _summary_cache.get(key)
    if cached is not None:
        return cached
    lines: list[str] = []
    for item in resume.items:
        detail = " ".join(item.detail.split())
        line = f"- {item.id}: {item.title}" + (f" -- {detail[:160]}" if detail else "")
        lines.append(line[:220])
    summary = "\n".join(lines)
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "\n…[truncated]"
    _summary_cache[key] = summary
    return summary


def clear_compact_cache() -> None:
    """Drop cached summaries (tests only; production keys on content)."""
    _summary_cache.clear()
