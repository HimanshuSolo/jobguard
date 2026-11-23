"""Strip hidden text from HTML (Phase 3 guardrail).

Attackers hide instructions in what humans never see: HTML comments,
zero-width characters, and elements styled invisible (display:none,
visibility:hidden, zero font size, or same-color text on background). This
module removes the hiding places and reports what it removed; the LLM
pipeline (Phase 4) only ever sees the cleaned text.
"""

from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup, Comment
from pydantic import BaseModel, Field

_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\ufeff\u2060\u180e]")
_HIDDEN_STYLE_RE = re.compile(
    r"(display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0)",
    re.IGNORECASE,
)
_COLOR_RE = re.compile(r"(color|background(?:-color)?)\s*:\s*([^;]+)", re.IGNORECASE)


def _same_colors(style: str) -> bool:
    colors = [value.strip().lower() for _, value in _COLOR_RE.findall(style)]
    return len(colors) >= 2 and len(set(colors)) == 1


_MD_LINK_LINE_RE = re.compile(r"^\s*\[.*?\]\(https?://\S+\)\s*$")
_BARE_URL_LINE_RE = re.compile(r"^\s*https?://\S+\s*$")
_EMPTY_MD_LINK_RE = re.compile(r"\[\]\(https?://\S+?\)")


def _truncate_tokens(line: str, *, max_token: int, keep: int) -> str:
    """Truncate over-long spaceless tokens in place, preserving whitespace."""
    parts = re.split(r"(\s+)", line)
    return "".join(
        part[:keep] + "… [truncated]"
        if len(part) > max_token and part.strip() == part and " " not in part
        else part
        for part in parts
    )


def collapse_boilerplate(text: str, *, max_token: int = 200, keep: int = 120) -> str:
    """Remove tracking pixels and truncate over-long spaceless tokens.

    Empty-text markdown links (``[](url)``) are tracking pixels: delete
    them inline. Lines that are solely a long URL are redirectors: drop
    them. Remaining spaceless monsters (tokens, IDs) are truncated in
    place. Paragraphs survive untouched, and the original full links stay
    available to the parsers, which run before this step.
    """
    text = _EMPTY_MD_LINK_RE.sub("", text)
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) > 120 and (_MD_LINK_LINE_RE.match(line) or _BARE_URL_LINE_RE.match(line)):
            continue
        out.append(_truncate_tokens(line, max_token=max_token, keep=keep))
    return "\n".join(out)


def strip_hidden_text(html: str) -> HiddenTextReport:
    """Remove hidden content; return cleaned text plus what was removed."""
    removed: list[str] = []
    soup = BeautifulSoup(html, "html.parser")

    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        text = str(comment).strip()
        if text:
            removed.append(f"comment:{text[:120]}")
        comment.extract()

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    for tag in soup.find_all(style=True):
        style = str(tag.get("style", ""))
        if _HIDDEN_STYLE_RE.search(style) or _same_colors(style):
            snippet = tag.get_text(separator=" ", strip=True)[:120]
            if snippet:
                removed.append(f"hidden-style:{snippet}")
            tag.decompose()

    visible = soup.get_text(separator="\n")
    cleaned_lines: list[str] = []
    zero_width_hits = 0
    for line in visible.splitlines():
        scrubbed, count = _ZERO_WIDTH_RE.subn("", line)
        zero_width_hits += count
        normalized = unicodedata.normalize("NFKC", scrubbed)
        if normalized.strip():
            cleaned_lines.append(normalized.strip())
    if zero_width_hits:
        removed.append(f"zero-width:{zero_width_hits} chars")
    return HiddenTextReport(text="\n".join(cleaned_lines), removed=removed)


class HiddenTextReport(BaseModel):
    """Cleaned text plus a description of every removal (for audit)."""

    text: str = ""
    removed: list[str] = Field(default_factory=list)
