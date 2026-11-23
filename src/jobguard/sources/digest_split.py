"""Multi-job digest splitter (Phase 2 hardening from real-world mail).

Single-job template parsing (`parse_alert`) mangles digests: one email
carrying 20 jobs became one posting with the subject line as its role.
`split_digest_jobs` splits known digest shapes into one `AlertPosting` per
job BEFORE normalization, so each job gets its own canonical key, score,
and suggestion. Unknown shapes fall back to single parsing.
"""

from __future__ import annotations

import re

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


def split_digest_jobs(sender: str, subject: str, body_text: str) -> list[AlertPosting]:
    """Split a digest email into per-job postings (platform-specific)."""
    platform = detect_platform(sender)
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
