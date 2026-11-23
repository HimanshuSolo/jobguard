"""RSS / curated-feed adapter (allowlisted feeds, SSRF-safe fetch)."""

from __future__ import annotations

import calendar
import time
from collections.abc import Collection
from dataclasses import dataclass, field

import feedparser
import httpx
from feedparser import FeedParserDict

from jobguard.sources.base import SourceItem
from jobguard.sources.http import Resolver, fetch_url_safe


def entry_published_iso(entry: FeedParserDict) -> str:
    """Entry date as ISO-8601 (feedparser structs beat raw strings, which sort badly)."""
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                stamp = calendar.timegm(time.struct_time(parsed))
                return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))
            except (TypeError, ValueError, OverflowError):
                continue
    return str(entry.get("published", "") or entry.get("updated", ""))


@dataclass(frozen=True)
class RssFeed:
    """One allowlisted feed."""

    name: str
    url: str


@dataclass
class RssAdapter:
    """Fetch entries from configured RSS/Atom feeds."""

    feeds: list[RssFeed] = field(default_factory=list)
    allowed_domains: Collection[str] = ()
    timeout_s: float = 10.0
    client: httpx.Client | None = None
    resolve: Resolver | None = None

    @property
    def source_name(self) -> str:
        return "rss"

    def fetch(self, since: str) -> list[SourceItem]:
        items: list[SourceItem] = []
        for feed in self.feeds:
            body = fetch_url_safe(
                feed.url,
                allowed_domains=self.allowed_domains,
                timeout_s=self.timeout_s,
                client=self.client,
                resolve=self.resolve,
            )
            parsed = feedparser.parse(body)
            for entry in parsed.entries:
                link = getattr(entry, "link", "")
                external_id = getattr(entry, "id", "") or link
                published = entry_published_iso(entry)
                if since and published and published < since:
                    continue
                items.append(
                    SourceItem(
                        source=f"rss:{feed.name}",
                        external_id=str(external_id),
                        payload={
                            "feed": feed.name,
                            "title": getattr(entry, "title", ""),
                            "link": link,
                            "published": published,
                            "summary": getattr(entry, "summary", ""),
                        },
                    )
                )
        return items

    def cursor_after(self, items: list[SourceItem], since: str) -> str:
        if not items:
            return since
        stamps = sorted(i.payload.get("published", "") or "" for i in items)
        return stamps[-1] or since


def parse_feeds_setting(value: str) -> tuple[list[RssFeed], list[str]]:
    """Parse ``name::url,name::url`` setting into feeds + their domains."""
    from urllib.parse import urlparse

    feeds: list[RssFeed] = []
    domains: list[str] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk or "::" not in chunk:
            continue
        name, url = chunk.split("::", 1)
        name, url = name.strip(), url.strip()
        parsed = urlparse(url)
        if not name or parsed.scheme not in ("http", "https") or not parsed.hostname:
            continue
        feeds.append(RssFeed(name=name, url=url))
        host = parsed.hostname or ""
        if host not in domains:
            domains.append(host)
    return feeds, domains
