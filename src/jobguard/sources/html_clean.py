"""HTML cleaning for emails and descriptions (Phase 2: extraction only).

Hidden-text stripping (white-on-white, zero-width, comments) is a Phase 3
guardrail; here we only turn messy HTML into readable text while preserving
links, which the parsers and normalizer need.
"""

from __future__ import annotations

import html2text
from bs4 import BeautifulSoup


def _sanitize(html: object) -> str:
    """Drop lone surrogates (surrogateescape artifacts) before parsing."""
    if not isinstance(html, str):
        return ""
    return html.encode("utf-8", errors="ignore").decode("utf-8")


def extract_text(html: str) -> str:
    """Best-effort visible text from HTML (scripts/styles dropped)."""
    soup = BeautifulSoup(_sanitize(html), "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    lines = [line.strip() for line in soup.get_text(separator="\n").splitlines()]
    return "\n".join(line for line in lines if line)


def html_to_markdown(html: str) -> str:
    """HTML to plain text with links preserved as ``[text](url)``."""

    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_images = True
    return converter.handle(_sanitize(html)).strip()
