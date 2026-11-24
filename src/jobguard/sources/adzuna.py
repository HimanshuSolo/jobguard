"""Adzuna India job-search API adapter (optional, official API).

``GET https://api.adzuna.com/v1/api/jobs/in/search/{page}`` with
``app_id``/``app_key`` (free developer key from developer.adzuna.com) returns
``{"results": [{id, title, company: {display_name}, location: {display_name,
area}, redirect_url, description, created}]}``. It aggregates postings from
thousands of Indian employers and job sites, which company boards alone
cannot cover. Disabled unless ``ADZUNA_APP_ID`` and ``ADZUNA_APP_KEY`` are set.

Like boards, results are snapshots: idempotency comes from ``emails_seen``;
``max_days_old`` bounds each query instead of a cursor. One failing query
never hides the others.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from jobguard.sources.base import SourceItem
from jobguard.sources.http import Resolver, fetch_url_safe

ADZUNA_DOMAINS = ("api.adzuna.com",)
_TAG_RE = re.compile(r"<[^>]+>")


def _plain(value: object) -> str:
    return _TAG_RE.sub("", str(value or "")).strip()


@dataclass
class AdzunaAdapter:
    """Run each configured query against Adzuna's India index."""

    app_id: str
    app_key: str
    queries: list[str] = field(default_factory=list)
    pages: int = 2
    results_per_page: int = 50
    max_days_old: int = 3
    allowed_domains: Collection[str] = ADZUNA_DOMAINS
    timeout_s: float = 15.0
    client: httpx.Client | None = None
    resolve: Resolver | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def source_name(self) -> str:
        return "adzuna"

    def _url(self, query: str, page: int) -> str:
        params = {
            "app_id": self.app_id,
            "app_key": self.app_key,
            "results_per_page": self.results_per_page,
            "what": query,
            "max_days_old": self.max_days_old,
            "sort_by": "date",
            "content-type": "application/json",
        }
        return f"https://api.adzuna.com/v1/api/jobs/in/search/{page}?{urlencode(params)}"

    def map_result(self, result: dict[str, Any]) -> SourceItem:
        company = result.get("company") or {}
        location = result.get("location") or {}
        return SourceItem(
            source="board:adzuna:in",
            external_id=str(result.get("id", "")),
            payload={
                "company": _plain(company.get("display_name", ""))
                if isinstance(company, dict)
                else "",
                "title": _plain(result.get("title", "")),
                "location": _plain(location.get("display_name", ""))
                if isinstance(location, dict)
                else "",
                "url": str(result.get("redirect_url", "")),
                "published": str(result.get("created", "")),
                "description_text": _plain(result.get("description", ""))[:4000],
            },
        )

    def fetch(self, since: str) -> list[SourceItem]:
        items: dict[str, SourceItem] = {}
        self.errors = []
        attempts = 0
        for query in self.queries:
            for page in range(1, self.pages + 1):
                attempts += 1
                try:
                    body = fetch_url_safe(
                        self._url(query, page),
                        allowed_domains=self.allowed_domains,
                        timeout_s=self.timeout_s,
                        client=self.client,
                        resolve=self.resolve,
                    )
                    data = json.loads(body.decode("utf-8"))
                except Exception as exc:  # never log the URL: it holds the key
                    self.errors.append(f"{query!r} p{page}: {type(exc).__name__}")
                    break
                results = data.get("results", []) if isinstance(data, dict) else []
                for result in results:
                    if isinstance(result, dict) and result.get("id") is not None:
                        item = self.map_result(result)
                        items.setdefault(item.external_id, item)
                if len(results) < self.results_per_page:
                    break
        if attempts and len(self.errors) == len(self.queries):
            raise RuntimeError(f"every Adzuna query failed: {self.errors[:3]}")
        return list(items.values())

    def cursor_after(self, items: list[SourceItem], since: str) -> str:
        return datetime.now(UTC).isoformat()
