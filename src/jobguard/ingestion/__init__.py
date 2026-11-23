"""Ingestion: normalize, dedupe, prefilter, idempotent sync."""

from jobguard.ingestion.dedupe import merge_duplicates, merge_pair
from jobguard.ingestion.normalize import (
    NormalizedPosting,
    canonical_key,
    key_of,
    normalize_item,
    normalize_text,
    normalize_url,
)
from jobguard.ingestion.prefilter import DEFAULT_KEYWORDS, DEFAULT_SENDERS, Prefilter, keep
from jobguard.ingestion.status import (
    RETRYABLE_STATUSES,
    TERMINAL_STATUSES,
    backfill_statuses,
    summarize,
)
from jobguard.ingestion.sync import SyncResult, sync_source
from jobguard.ingestion.triage import TriageResult, triage_posting

__all__ = [
    "DEFAULT_KEYWORDS",
    "DEFAULT_SENDERS",
    "NormalizedPosting",
    "Prefilter",
    "RETRYABLE_STATUSES",
    "SyncResult",
    "TERMINAL_STATUSES",
    "TriageResult",
    "backfill_statuses",
    "canonical_key",
    "keep",
    "key_of",
    "merge_duplicates",
    "merge_pair",
    "normalize_item",
    "normalize_text",
    "normalize_url",
    "summarize",
    "sync_source",
    "triage_posting",
]
