"""Public company job-board adapters (Greenhouse / Lever JSON endpoints).

Formats verified against current public API behavior (see architecture docs):
- Greenhouse: ``GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs``
  returns ``{"jobs": [{id, title, location: {name}, absolute_url, ...}]}``;
  ``?content=true`` adds an HTML ``content`` field but balloons the response,
  so it is opt-in per board.
- Lever: ``GET https://api.lever.co/v0/postings/{slug}?mode=json`` returns a
  top-level array of ``{id, text, categories: {location,...}, hostedUrl,
  descriptionPlain, createdAt}``.
- Ashby: ``GET https://api.ashbyhq.com/posting-api/job-board/{slug}`` returns
  ``{"jobs": [{id, title, location, secondaryLocations, jobUrl,
  descriptionPlain, publishedAt, isListed, ...}]}`` (needs a User-Agent).

Boards are snapshots: every fetch returns the full current list, and
idempotency comes from ``emails_seen`` in the sync, not from a date cursor.
(A date cursor silently dropped every job on a newly added board whose
publish date predated the last run.) One board failing never hides the
others: failures are collected in ``errors`` and the rest still return.

These are official public JSON endpoints, not scraping. The company list is
configured in ``data/boards.yaml`` (see ``data/boards.example.yaml``).
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
import yaml

from jobguard.sources.base import SourceItem
from jobguard.sources.http import Resolver, fetch_url_safe

BOARD_DOMAINS = ("boards-api.greenhouse.io", "api.lever.co", "api.ashbyhq.com")
Provider = Literal["greenhouse", "lever", "ashby"]


@dataclass(frozen=True)
class BoardSpec:
    """One company's public board."""

    provider: Provider
    slug: str
    company: str = ""
    include_content: bool = False


@dataclass
class BoardFeedAdapter:
    """Fetch every configured board. Snapshots: ``since`` filters by date."""

    boards: list[BoardSpec] = field(default_factory=list)
    allowed_domains: Collection[str] = BOARD_DOMAINS
    timeout_s: float = 15.0
    max_bytes: int = 8_000_000
    client: httpx.Client | None = None
    resolve: Resolver | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def source_name(self) -> str:
        return "boards"

    def _endpoint(self, board: BoardSpec) -> str:
        if board.provider == "greenhouse":
            url = f"https://boards-api.greenhouse.io/v1/boards/{board.slug}/jobs"
            return url + ("?content=true" if board.include_content else "")
        if board.provider == "ashby":
            return f"https://api.ashbyhq.com/posting-api/job-board/{board.slug}"
        return f"https://api.lever.co/v0/postings/{board.slug}?mode=json"

    def map_greenhouse(self, board: BoardSpec, job: dict[str, Any]) -> SourceItem:
        location = job.get("location") or {}
        return SourceItem(
            source=f"board:greenhouse:{board.slug}",
            external_id=str(job.get("id", "")),
            payload={
                "company": board.company or job.get("company_name", ""),
                "title": job.get("title", ""),
                "location": location.get("name", "") if isinstance(location, dict) else "",
                "url": job.get("absolute_url", ""),
                "published": job.get("first_published", ""),
                "updated": job.get("updated_at", ""),
                "description_html": job.get("content", "") if board.include_content else "",
            },
        )

    def map_lever(self, board: BoardSpec, posting: dict[str, Any]) -> SourceItem:
        categories = posting.get("categories") or {}
        created_ms = posting.get("createdAt", "")
        published = ""
        if isinstance(created_ms, (int, float)):
            published = datetime.fromtimestamp(created_ms / 1000, UTC).isoformat()
        return SourceItem(
            source=f"board:lever:{board.slug}",
            external_id=str(posting.get("id", "")),
            payload={
                "company": board.company,
                "title": posting.get("text", ""),
                "location": categories.get("location", "") if isinstance(categories, dict) else "",
                "url": posting.get("hostedUrl", ""),
                "published": published,
                "description_text": posting.get("descriptionPlain", ""),
                "description_html": posting.get("description", ""),
            },
        )

    def map_ashby(self, board: BoardSpec, job: dict[str, Any]) -> SourceItem:
        locations = [str(job.get("location", "") or "")]
        for extra in job.get("secondaryLocations") or []:
            if isinstance(extra, dict) and extra.get("location"):
                locations.append(str(extra["location"]))
        if job.get("isRemote") and not any("remote" in x.casefold() for x in locations):
            locations.append("Remote")
        return SourceItem(
            source=f"board:ashby:{board.slug}",
            external_id=str(job.get("id", "")),
            payload={
                "company": board.company or board.slug,
                "title": job.get("title", ""),
                "location": "; ".join(x for x in locations if x),
                "url": job.get("jobUrl", "") or job.get("applyUrl", ""),
                "published": job.get("publishedAt", ""),
                "description_text": str(job.get("descriptionPlain", "") or "")[:6000],
            },
        )

    def _fetch_board(self, board: BoardSpec) -> list[SourceItem]:
        body = fetch_url_safe(
            self._endpoint(board),
            allowed_domains=self.allowed_domains,
            timeout_s=self.timeout_s,
            max_bytes=self.max_bytes,
            client=self.client,
            resolve=self.resolve,
        )
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return []
        items: list[SourceItem] = []
        if board.provider == "greenhouse":
            jobs = data.get("jobs", []) if isinstance(data, dict) else []
            items = [self.map_greenhouse(board, j) for j in jobs if isinstance(j, dict)]
        elif board.provider == "ashby":
            jobs = data.get("jobs", []) if isinstance(data, dict) else []
            items = [
                self.map_ashby(board, j)
                for j in jobs
                if isinstance(j, dict) and j.get("isListed", True) is not False
            ]
        else:
            postings = data if isinstance(data, list) else []
            items = [self.map_lever(board, p) for p in postings if isinstance(p, dict)]
        return items

    def fetch(self, since: str) -> list[SourceItem]:
        """Full snapshot of every board; ``since`` is ignored (see module doc)."""
        items: list[SourceItem] = []
        self.errors = []
        for board in self.boards:
            try:
                items.extend(self._fetch_board(board))
            except Exception as exc:  # one bad board must not hide the rest
                self.errors.append(f"{board.provider}:{board.slug}: {type(exc).__name__}")
        if self.boards and len(self.errors) == len(self.boards):
            raise RuntimeError(f"every board failed: {self.errors[:3]}")
        return items

    def cursor_after(self, items: list[SourceItem], since: str) -> str:
        return datetime.now(UTC).isoformat()


def load_boards(path: str) -> list[BoardSpec]:
    """Load the company board list from a YAML file (see boards.example.yaml)."""
    from pathlib import Path

    if not Path(path).exists():
        return []
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    boards: list[BoardSpec] = []
    for entry in raw.get("boards", []):
        if entry.get("provider") in ("greenhouse", "lever", "ashby") and entry.get("slug"):
            boards.append(
                BoardSpec(
                    provider=entry["provider"],
                    slug=str(entry["slug"]),
                    company=str(entry.get("company", "")),
                    include_content=bool(entry.get("include_content", False)),
                )
            )
    return boards
