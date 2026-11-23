"""Input guardrails: hidden text, injection, PII, links, scam heuristics."""

from jobguard.guardrails.hidden import HiddenTextReport, strip_hidden_text
from jobguard.guardrails.injection import (
    InjectionFinding,
    InjectionReport,
    scan_injection,
)
from jobguard.guardrails.links import FlaggedLink, LinkReport, check_links, extract_urls
from jobguard.guardrails.pii import PiiReport, redact_pii
from jobguard.guardrails.scam import SCAM_THRESHOLD, ScamReport, ScamSignal, check_scam

__all__ = [
    "SCAM_THRESHOLD",
    "FlaggedLink",
    "HiddenTextReport",
    "InjectionFinding",
    "InjectionReport",
    "LinkReport",
    "PiiReport",
    "ScamReport",
    "ScamSignal",
    "check_links",
    "check_scam",
    "extract_urls",
    "redact_pii",
    "scan_injection",
    "strip_hidden_text",
]
