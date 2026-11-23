"""Tests for board + RSS adapters over fixture payloads (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from jobguard.sources.boards import BoardFeedAdapter, BoardSpec, load_boards
from jobguard.sources.rss import RssAdapter, RssFeed, entry_published_iso, parse_feeds_setting

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_client() -> httpx.Client:
    greenhouse = (FIXTURES / "boards" / "greenhouse.json").read_bytes()
    lever = (FIXTURES / "boards" / "lever.json").read_bytes()
    rss = (FIXTURES / "feeds" / "careers.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "boards-api.greenhouse.io":
            return httpx.Response(200, content=greenhouse)
        if host == "api.lever.co":
            return httpx.Response(200, content=lever)
        if host == "careers.example.com":
            return httpx.Response(200, content=rss)
        return httpx.Response(404, content=b"nope")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_greenhouse_mapping() -> None:
    adapter = BoardFeedAdapter(
        boards=[BoardSpec(provider="greenhouse", slug="example-co", company="Example Co")],
        client=_fixture_client(),
        resolve=lambda host: ["93.184.216.34"],
    )
    items = adapter.fetch("")
    assert len(items) == 2
    first = items[0]
    assert first.source == "board:greenhouse:example-co"
    assert first.payload["title"] == "Software Engineer Intern"
    assert first.payload["location"] == "Bengaluru, Karnataka"
    assert first.payload["url"].endswith("/jobs/101")


def test_lever_mapping() -> None:
    adapter = BoardFeedAdapter(
        boards=[BoardSpec(provider="lever", slug="example-labs", company="Example Labs")],
        client=_fixture_client(),
        resolve=lambda host: ["93.184.216.34"],
    )
    items = adapter.fetch("")
    assert len(items) == 2
    first = items[0]
    assert first.payload["title"] == "SDE Intern"
    assert first.payload["description_text"].startswith("6-month")
    assert first.payload["published"].startswith("2026-")


def test_boards_since_filters() -> None:
    adapter = BoardFeedAdapter(
        boards=[
            BoardSpec(provider="greenhouse", slug="example-co", company="Example Co"),
            BoardSpec(provider="lever", slug="example-labs", company="Example Labs"),
        ],
        client=_fixture_client(),
        resolve=lambda host: ["93.184.216.34"],
    )
    items = adapter.fetch("2026-11-21")
    # Only the Lever posting published 2026-11-21 survives the cutoff.
    assert [i.external_id for i in items] == ["aaa-111"]


def test_load_boards_missing_file() -> None:
    assert load_boards("does-not-exist.yaml") == []


def test_load_boards_example_shape(tmp_path: Path) -> None:
    path = tmp_path / "boards.yaml"
    path.write_text(
        "boards:\n"
        "  - {provider: greenhouse, slug: acme, company: Acme}\n"
        "  - {provider: bogus, slug: x}\n",
        encoding="utf-8",
    )
    boards = load_boards(str(path))
    assert boards == [BoardSpec(provider="greenhouse", slug="acme", company="Acme")]


def test_rss_entries() -> None:
    adapter = RssAdapter(
        feeds=[RssFeed(name="careers", url="https://careers.example.com/feed.xml")],
        allowed_domains=("careers.example.com",),
        client=_fixture_client(),
        resolve=lambda host: ["93.184.216.34"],
    )
    items = adapter.fetch("")
    assert len(items) == 2
    assert items[0].payload["title"].startswith("Backend Developer Intern")
    assert items[0].payload["published"].startswith("2026-11-23")
    # RFC822 strings would sort wrong ("Mon" < "Sun"); ISO must not.
    assert adapter.cursor_after(items, "") == items[0].payload["published"]
    recent_only = adapter.fetch("2026-11-23")
    assert len(recent_only) == 1


def test_entry_published_iso_fallback() -> None:
    from feedparser import FeedParserDict

    entry = FeedParserDict({"published": "sometime"})
    assert entry_published_iso(entry) == "sometime"


def test_parse_feeds_setting() -> None:
    feeds, domains = parse_feeds_setting("careers::https://careers.example.com/f, x::bogus, bad")
    assert feeds == [RssFeed(name="careers", url="https://careers.example.com/f")]
    assert domains == ["careers.example.com"]
    assert parse_feeds_setting("") == ([], [])


def test_fixtures_are_valid_json() -> None:
    json.loads((FIXTURES / "boards" / "greenhouse.json").read_text(encoding="utf-8"))
    json.loads((FIXTURES / "boards" / "lever.json").read_text(encoding="utf-8"))
