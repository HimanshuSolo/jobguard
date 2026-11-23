"""Persistent LLM response cache (Phase 9, Step 2.8).

Key = sha256(model family, prompt version, cleaned content). A hit makes
zero network calls and is not charged to the token budget. Entries live in
the ``llm_cache`` table, so interrupted or throttled runs resume from cache
without duplicate calls or duplicate suggestions. A prompt-version bump
invalidates every entry.

The helpers are session-bound: callers pass the item's own session, so no
cache state is shared across pool threads.
"""

from __future__ import annotations

import hashlib
import threading

from sqlalchemy.orm import Session

from jobguard.store.models import LLMCache

# Serializes check-then-insert across pool threads: without it, two workers
# serving identical content both miss and both INSERT the same key.
_PUT_LOCK = threading.Lock()


def cache_key(model_family: str, prompt_version: str, content: str) -> str:
    """Stable cache key for one LLM call's cleaned content."""
    material = "\n".join((model_family, prompt_version, content))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def cache_get(session: Session, key: str) -> str | None:
    """Return the cached response text, or None on a miss."""
    row = session.get(LLMCache, key)
    return row.response_text if row is not None else None


def cache_put(
    session: Session,
    *,
    key: str,
    model_family: str,
    prompt_version: str,
    response_text: str,
) -> None:
    """Store a validated response, committing immediately.

    The row is independent of the item's transaction, so it commits on its
    own: a later interrupt still resumes from cache. The check, insert, and
    commit hold a process-wide lock so concurrent pool workers serving
    identical content cannot both INSERT the same key.
    """
    with _PUT_LOCK:
        if session.get(LLMCache, key) is None:
            session.add(
                LLMCache(
                    key=key,
                    model_family=model_family,
                    prompt_version=prompt_version,
                    response_text=response_text,
                )
            )
            session.commit()
