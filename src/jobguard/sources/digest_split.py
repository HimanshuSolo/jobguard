"""Multi-job digest splitter (Phase 2 hardening from real-world mail).

Single-job template parsing (`parse_alert`) mangles digests: one email
carrying 20 jobs became one posting with the subject line as its role.
`split_digest_jobs` splits known digest shapes into one `AlertPosting` per
job BEFORE normalization, so each job gets its own canonical key, score,
and suggestion. Unknown shapes fall back to single parsing.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit, urlunsplit

from jobguard.relevance import INDIA_TERMS
from jobguard.sources.parsers import URL_RE, AlertPosting, detect_platform, parse_alert

_DASH_RE = re.compile(r"^\s*-{5,}\s*$", re.MULTILINE)
_VIEW_JOB_RE = re.compile(r"(?i)view job:\s*(\S+)")
_COMPANY_LOCATION_RE = re.compile(r"^([^-\n]{1,80}?) - ([^-\n]{1,80})$")
_BADGE_WORDS = (
    "easily apply",
    "just posted",
    "actively hiring",
    "act now",
    "apply now",
    "promoted",
    "sponsored",
)
_HEADER_MARKERS = (
    "expand your search",
    "recommendations based on",
    "jobs ",
    "matching results on",
    "see matching",
)
_SALARY_RE = re.compile(r"(₹|\$|€|£|salary|per hour|an hour|a month|per month|lakh|/ ?yr)")


def _clean_url(url: str) -> str:
    return url.rstrip(".,;)")


def _is_linkedin_noise(line: str) -> bool:
    """Header, search, or separator lines that never belong to a job block."""
    lowered = line.casefold()
    if not line or "http" in line or _DASH_RE.match(line):
        return True
    return any(marker in lowered for marker in _HEADER_MARKERS)


def split_linkedin_jobs(sender: str, subject: str, body: str) -> list[AlertPosting]:
    """Split LinkedIn digests at each `View job:` line.

    Every job block ends with its apply link; walking backwards yields
    Location / Company / Title. Header and search lines are noise and stop
    the walk, so leading headers can share a chunk with the first job.
    """
    lines = [line.strip() for line in body.splitlines()]
    postings: list[AlertPosting] = []
    for index, line in enumerate(lines):
        link_match = _VIEW_JOB_RE.search(line)
        if not link_match:
            continue
        collected: list[str] = []
        back = index - 1
        while back >= 0 and len(collected) < 3:
            candidate = lines[back]
            if not candidate or _VIEW_JOB_RE.search(candidate):
                break
            if _is_linkedin_noise(candidate):
                break
            collected.append(candidate)
            back -= 1
        if len(collected) < 2:
            continue
        collected.reverse()  # [role, company] or [role, company, location]
        role = collected[-3] if len(collected) == 3 else collected[-2]
        company = collected[-2] if len(collected) == 3 else collected[-1]
        location = collected[-1] if len(collected) == 3 else ""
        postings.append(
            AlertPosting(
                platform="linkedin",
                company=company[:200],
                role=role[:200],
                location=location[:200],
                link=_clean_url(link_match.group(1)),
                sender=sender,
                subject=subject,
            )
        )
    return postings


def _is_company_location(line: str) -> re.Match[str] | None:
    lowered = line.casefold()
    if any(badge in lowered for badge in _BADGE_WORDS):
        return None
    if len(line) > 80 or "http" in line:
        return None
    if _SALARY_RE.search(lowered):
        return None
    return _COMPANY_LOCATION_RE.match(line)


def split_indeed_jobs(sender: str, subject: str, body: str) -> list[AlertPosting]:
    """Split Indeed digests: Title line + `Company - Location` line, repeating."""
    lines = [line.strip() for line in body.splitlines()]
    # Skip the digest header (everything through the "Jobs N-M of ..." line).
    start = 0
    for index, line in enumerate(lines):
        if re.match(r"(?i)^jobs \d+", line):
            start = index + 1
            break
    postings: list[AlertPosting] = []
    index = start
    while index < len(lines):
        line = lines[index]
        match = _is_company_location(line) if line else None
        if match and index > start:
            # Title = nearest previous non-empty, non-badge line.
            back = index - 1
            while back >= start and (not lines[back] or _is_company_location(lines[back])):
                back -= 1
            role = lines[back] if back >= start else ""
            if not role or role.casefold() in _HEADER_MARKERS:
                index += 1
                continue
            # Link = first URL in following lines until the next job block.
            link = ""
            look = index + 1
            while look < len(lines):
                if _is_company_location(lines[look]) and look > index + 1:
                    break
                url_match = URL_RE.search(lines[look])
                if url_match and not link:
                    link = _clean_url(url_match.group(0))
                    break
                look += 1
            company, location = match.group(1).strip(), match.group(2).strip()
            postings.append(
                AlertPosting(
                    platform="indeed",
                    company=company[:200],
                    role=role[:200],
                    location=location[:200],
                    link=link,
                    sender=sender,
                    subject=subject,
                )
            )
        index += 1
    return postings


# --- Link-card digests (Naukri, Glassdoor, Hirist) -------------------------
# These mails wrap every job in its own tracked link carrying a stable job
# id, so the split keys on that id instead of on fragile line templates.

_MD_LINK_END = re.compile(r"\]\((https?://[^\s)]+)\)")
_INNER_LINK = re.compile(r"!?\[([^\[\]]*)\]\([^)]*\)")
_INVISIBLE = re.compile("[\u00ad\u034f\u200b-\u200f\u2060\ufeff]")
_RATING_TAIL = re.compile(r"\s+\d(?:\.\d)?\s*★?$")
_NOISE_RES = (
    re.compile(r"^\d(?:\.\d)?\s*★?$"),  # rating
    re.compile(r"^\d+\s*[dh]\+?$"),  # age, e.g. 8d / 24h
    re.compile(r"(?i)^\d+\s*(?:-|to)\s*\d+\s*(?:yrs?|years?)$"),  # experience
    re.compile(r"(?i)(₹|\$|lpa|lakh|est\.\)|per month|/month|stipend)"),  # pay
    re.compile(r"(?i)^(?:\d+\s*months?\s*duration|no fixed duration)$"),
    re.compile(
        r"(?i)^(?:easy apply|internship|full[ -]?time|part[ -]?time|new|hot job|"
        r"actively hiring|be an early applicant|apply now|view job|job|jobs|"
        r"work from home|fresher)$"
    ),
)

_AGE_RE = re.compile(r"(?:^|[\s|])(\d{1,2})\s*([dh])\+?\s*(?=$|[\s|\]])", re.MULTILINE)

_CARD_PLATFORMS: dict[str, tuple[re.Pattern[str], re.Pattern[str]]] = {
    # platform: (job-link matcher, job-id extractor)
    "naukri": (re.compile(r"naukri\.com/jd/"), re.compile(r"-(\d{9,})(?:[?#]|$)")),
    "glassdoor": (
        re.compile(r"glassdoor\.[a-z.]+/(?:partner/jobListing\.htm|job-listing/)"),
        re.compile(r"(?:jobListingId|jl)=(\d+)"),
    ),
    "hirist": (
        re.compile(r"hirist\.(?:tech|com)(?:/|%2F)j(?:/|%2F)", re.IGNORECASE),
        re.compile(r"-(\d{5,})(?:%3F|\?|/|$)", re.IGNORECASE),
    ),
}


def _card_platform(sender: str) -> str:
    lowered = sender.casefold()
    return next((p for p in _CARD_PLATFORMS if p in lowered), "")


def _iter_md_links(text: str) -> list[tuple[str, str]]:
    """(link text, url) for every markdown link, nested links included."""
    links: list[tuple[str, str]] = []
    for match in _MD_LINK_END.finditer(text):
        close = match.start()
        depth, index = 0, close - 1
        floor = max(-1, close - 4000)
        while index > floor:
            char = text[index]
            if char == "]":
                depth += 1
            elif char == "[":
                if depth == 0:
                    break
                depth -= 1
            index -= 1
        if index <= floor:
            continue
        links.append((text[index + 1 : close], match.group(1)))
    return links


def _card_lines(text: str) -> list[str]:
    text = _INVISIBLE.sub("", text)
    for _ in range(3):  # unwrap nested links/images to their text
        text = _INNER_LINK.sub(r"\1", text)
    lines: list[str] = []
    for raw in re.split(r"[\n|·]", text):
        line = raw.strip(" \t*_[]()-—–:.")
        if not line or not re.search(r"[A-Za-z]", line):
            continue
        if any(rx.search(line) for rx in _NOISE_RES):
            continue
        if line not in lines:
            lines.append(line)
    return lines


def _clean_link(platform: str, url: str) -> str:
    if platform == "hirist":
        embedded = re.search(r"https?:%2F%2F[^/]+%2Fj%2F[^%/]+", url, re.IGNORECASE)
        if embedded:
            return unquote(embedded.group(0))
    if platform == "naukri":
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return url


def _location_from_slug(url: str) -> str:
    slug = urlsplit(url).path.replace("-", " ").casefold()
    for term in ("remote", *INDIA_TERMS):
        if re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", slug):
            return term.title()
    return ""


def split_link_cards(sender: str, subject: str, body: str) -> list[AlertPosting]:
    """Split Naukri / Glassdoor / Hirist digests on their per-job links."""
    platform = _card_platform(sender)
    if not platform:
        return []
    link_re, id_re = _CARD_PLATFORMS[platform]
    order: list[str] = []
    texts: dict[str, list[str]] = {}
    urls: dict[str, str] = {}
    for text, url in _iter_md_links(body):
        if not link_re.search(url):
            continue
        id_match = id_re.search(url)
        if not id_match:
            continue
        job_id = id_match.group(1)
        if job_id not in texts:
            order.append(job_id)
            texts[job_id] = []
            urls[job_id] = url
        texts[job_id].append(text)

    postings: list[AlertPosting] = []
    for job_id in order:
        variants = [t for t in texts[job_id] if t.strip()]
        if not variants:
            continue
        card = max(variants, key=len)
        lines = _card_lines(card)
        if not lines:
            continue
        role = company = location = ""
        age_days: int | None = None
        if platform == "naukri":
            # The title is its own short link; the card follows with company.
            short = [t.strip() for t in variants if "\n" not in t and 0 < len(t.strip()) < 150]
            role = min(short, key=len) if short else lines[0]
            # Cards repeat a (sometimes truncated) copy of the title: skip it.
            rest = [
                line
                for line in lines
                if not role.startswith(line.rstrip(". ")) and not line.startswith(role)
            ]
            company = rest[0].rstrip(". ") if rest else ""
            location = _location_from_slug(urls[job_id])
        elif platform == "glassdoor":
            # Card order: Company [rating] / Title / Location / pay / age.
            age_match = _AGE_RE.search(_INNER_LINK.sub(r"\1", card))
            if age_match:
                age_days = 0 if age_match.group(2) == "h" else int(age_match.group(1))
            company = _RATING_TAIL.sub("", lines[0])
            role = lines[1] if len(lines) > 1 else ""
            location = lines[2] if len(lines) > 2 else ""
        else:  # hirist: "Title Company · 1-2 Yrs · City" (title+company fused)
            parts = [p.strip() for p in re.split(r"·", _INNER_LINK.sub(r"\1", card)) if p.strip()]
            role = _card_lines(parts[0])[0] if parts and _card_lines(parts[0]) else lines[0]
            location = lines[-1] if len(lines) > 1 else ""
        if not role:
            continue
        postings.append(
            AlertPosting(
                platform=platform,
                company=company[:200],
                role=role[:200],
                location=location[:200],
                link=_clean_link(platform, urls[job_id]),
                sender=sender,
                subject=subject,
                snippet=" | ".join(lines)[:1500],
                age_days=age_days,
            )
        )
    return postings


def split_digest_jobs(sender: str, subject: str, body_text: str) -> list[AlertPosting]:
    """Split a digest email into per-job postings (platform-specific)."""
    platform = detect_platform(sender)
    if _card_platform(sender):
        jobs = split_link_cards(sender, subject, body_text)
        if jobs:
            return jobs
    if platform == "linkedin":
        jobs = split_linkedin_jobs(sender, subject, body_text)
        if jobs:
            return jobs
    elif platform == "indeed":
        jobs = split_indeed_jobs(sender, subject, body_text)
        if jobs:
            return jobs
    single = parse_alert(sender, subject, body_text)
    return [single] if single else []
