"""Outbound content scan (Phase 5).

The digest pipeline carries job text that may itself contain secrets (a
pasted API key in a description, a credential in a sample email). This scan
runs on the final subject + body BEFORE sending: any hit refuses the send
and audits. PII in the general sense is expected in a personal digest (it is
addressed to the owner about their own search), so only secret-shaped
material blocks.
"""

from __future__ import annotations

import re

import structlog

log = structlog.get_logger(__name__)

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "api-key",
        re.compile(
            r"(?i)\b(sk-[A-Za-z0-9]{8,}|xox[bap]-[A-Za-z0-9-]+|"
            r"ghp_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{16})\b"
        ),
    ),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{16,}={0,2}\b")),
    (
        "credential-assign",
        re.compile(r"(?i)\b(password|passwd|api[_-]?key|secret)\s*[:=]\s*\S{4,}"),
    ),
]


def scan_outbound(subject: str, body: str) -> list[str]:
    """Return hit rule names; empty means the content may be sent."""
    blob = f"{subject}\n{body}"
    hits = [name for name, pattern in _PATTERNS if pattern.search(blob)]
    if hits:
        log.info("notify.outbound_blocked", rules=sorted(set(hits)))
    return sorted(set(hits))
