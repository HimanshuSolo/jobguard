"""Public company job-board adapters (Greenhouse / Lever JSON endpoints).

Formats verified against current public API behavior (see architecture docs):
- Greenhouse: ``GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs``
  returns ``{"jobs": [{id, title, location: {name}, absolute_url, ...}]}``;
  ``?content=true`` adds an HTML ``content`` field but balloons the response,
  so it is opt-in per board.
- Lever: ``GET https://api.lever.co/v0/postings/{slug}?mode=json`` returns a
  top-level array of ``{id, text, categories: {location,...}, hostedUrl,
  descriptionPlain, createdAt}``.

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

BOARD_DOMAINS = ("boards-api.greenhouse.io", "api.lever.co")


@dataclass(frozen=True)
class BoardSpec:
    """One company's public board."""

    provider: Literal["greenhouse", "lever"]
    slug: str
    company: str = ""
    include_content: bool = False


@dataclass
class BoardFeedAdapter:
    """Fetch every configured board. Snapshots: ``since`` filters by date."""

    boards: list[BoardSpec] = field(default_factory=list)
    allowed_domains: Collection[str] = BOARD_DOMAINS
    timeout_s: float = 10.0
    client: httpx.Client | None = None
    resolve: Resolver | None = None

    @property
    def source_name(self) -> str:
        return "boards"

    def _endpoint(self, board: BoardSpec) -> str:
        if board.provider == "greenhouse":
            url = f"https://boards-api.greenhouse.io/v1/boards/{board.slug}/jobs"
            return url + ("?content=true" if board.include_content else "")
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

    def fetch(self, since: str) -> list[SourceItem]:
        items: list[SourceItem] = []
        for board in self.boards:
            body = fetch_url_safe(
                self._endpoint(board),
                allowed_domains=self.allowed_domains,
                timeout_s=self.timeout_s,
                client=self.client,
                resolve=self.resolve,
            )
            try:
                data = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if board.provider == "greenhouse":
                jobs = data.get("jobs", []) if isinstance(data, dict) else []
                for job in jobs:
                    if isinstance(job, dict):
                        items.append(self.map_greenhouse(board, job))
            else:
                postings = data if isinstance(data, list) else []
                for posting in postings:
                    if isinstance(posting, dict):
                        items.append(self.map_lever(board, posting))
        if since:
            items = [i for i in items if (i.payload.get("published", "") or "") >= since]
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
        if entry.get("provider") in ("greenhouse", "lever") and entry.get("slug"):
            boards.append(
                BoardSpec(
                    provider=entry["provider"],
                    slug=str(entry["slug"]),
                    company=str(entry.get("company", "")),
                    include_content=bool(entry.get("include_content", False)),
                )
            )
    return boards
