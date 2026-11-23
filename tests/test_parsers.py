"""Tests for alert-email template parsers, driven by synthetic fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jobguard.sources.html_clean import extract_text, html_to_markdown
from jobguard.sources.parsers import detect_platform, parse_alert

FIXTURES = Path(__file__).parent / "fixtures" / "emails"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "name",
    [
        "linkedin.json",
        "indeed.json",
        "naukri.json",
        "internshala.json",
        "unstop.json",
        "glassdoor.json",
        "company.json",
        "personal.json",
    ],
)
def test_fixture_parses_to_expectation(name: str) -> None:
    fixture = _load(name)
    parsed = parse_alert(str(fixture["sender"]), str(fixture["subject"]), str(fixture["body_text"]))
    expect = fixture["expect"]
    if expect is None:
        assert parsed is None  # linkless, labelless mail is not a posting
        return
    assert parsed is not None
    assert parsed.platform == expect["platform"]
    assert parsed.company == expect["company"]
    assert parsed.role == expect["role"]
    assert parsed.location == expect["location"]
    assert parsed.link == expect["link"]


def test_nothing_job_like_returns_none() -> None:
    assert parse_alert("a@b.example", "", "") is None


def test_detect_platform_unknown() -> None:
    assert detect_platform("careers@acme.example") == "unknown"
    assert detect_platform("jobs-noreply@linkedin.com") == "linkedin"


def test_extract_text_drops_scripts() -> None:
    html = "<html><head><script>alert(1)</script></head><body><p>Apply now</p></body></html>"
    text = extract_text(html)
    assert "Apply now" in text
    assert "alert(1)" not in text


def test_html_to_markdown_preserves_links() -> None:
    html = '<p>Apply <a href="https://acme.example/jobs/1">here</a></p>'
    md = html_to_markdown(html)
    assert "https://acme.example/jobs/1" in md
