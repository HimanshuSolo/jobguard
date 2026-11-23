"""Link extraction and checks (Phase 3).

Every URL in untrusted content is inspected: non-http(s) schemes, raw IP
hosts, non-ASCII (homograph/punycode) hosts, and absurd length are flagged.
The report never fetches anything — fetching happens only through the
SSRF-safe fetcher with an allowlist (Phase 2 / policy `fetch_job_url`).
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from jobguard.sources.parsers import URL_RE

_MAX_URL_LEN = 2048


class FlaggedLink(BaseModel):
    """One URL plus the machine-readable reason it was flagged."""

    url: str
    reason: str


class LinkReport(BaseModel):
    """All URLs found, split into safe and flagged."""

    safe: list[str] = Field(default_factory=list)
    flagged: list[FlaggedLink] = Field(default_factory=list)


def extract_urls(text: str) -> list[str]:
    """All http(s) URLs in text (trailing punctuation stripped)."""
    return [m.group(0).rstrip(".,;)") for m in URL_RE.finditer(text)]


def check_links(text: str) -> LinkReport:
    """Classify every URL in ``text`` as safe or flagged (no fetching)."""
    report = LinkReport()
    for url in extract_urls(text):
        try:
            parsed = urlparse(url)
        except ValueError:
            report.flagged.append(FlaggedLink(url=url[:200], reason="unparseable"))
            continue
        host = parsed.hostname or ""
        if len(url) > _MAX_URL_LEN:
            report.flagged.append(FlaggedLink(url=url[:200], reason="too-long"))
        elif not host:
            report.flagged.append(FlaggedLink(url=url[:200], reason="no-host"))
        elif _is_ip(host):
            report.flagged.append(FlaggedLink(url=url[:200], reason="ip-host"))
        elif not host.isascii():
            report.flagged.append(FlaggedLink(url=url[:200], reason="non-ascii-host"))
        elif "xn--" in host:
            report.flagged.append(FlaggedLink(url=url[:200], reason="punycode-host"))
        else:
            report.safe.append(url)
    return report


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False
