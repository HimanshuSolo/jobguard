"""Relevance gates: is this a developer role, and is it open in India?

Both gates are deterministic and free, so they run before any LLM spend and
again at digest time. They are deliberately word-boundary based: plain
substring matching let ``ml`` match "html" and ``intern`` match
"international", which let almost anything through.

Location policy (``india``): keep postings located in India, remote roles
not pinned to another country, and postings with no location at all (the
LLM/user decides those). Drop everything that names only places outside
India. Unknown small Indian towns can be added via the profile's
``locations`` list.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Role words that make a title a software/developer role.
DEFAULT_ROLE_KEYWORDS = (
    "sde",
    "sdet",
    "swe",
    "software",
    "developer",
    "engineer",
    "engineering",
    "programmer",
    "coder",
    "backend",
    "back end",
    "back-end",
    "frontend",
    "front end",
    "front-end",
    "full stack",
    "full-stack",
    "fullstack",
    "devops",
    "sre",
    "web",
    "mobile",
    "android",
    "ios",
    "flutter",
    "react",
    "node",
    "python",
    "java",
    "golang",
    "rust",
    "ml",
    "ai",
    "machine learning",
    "data",
    "cloud",
    "qa",
    "test automation",
)

# Non-engineering functions: a title naming one of these is not a dev role
# even if it also says "software" (e.g. "IT / Software Sales Executive").
NON_DEV_ROLE_TERMS = (
    "sales",
    "marketing",
    "recruiter",
    "recruitment",
    "talent acquisition",
    "human resources",
    "hr",
    "business development",
    "account manager",
    "account executive",
    "accountant",
    "accounts",
    "finance",
    "audit",
    "legal",
    "content writer",
    "copywriter",
    "customer support",
    "customer success",
    "telecaller",
    "data entry",
    "operations executive",
    "graphic designer",
    "strategy",
    "user research",
    "researcher",
    "product manager",
    "product designer",
)

INDIA_TERMS = (
    "india",
    "pan india",
    "bengaluru",
    "bangalore",
    "gurugram",
    "gurgaon",
    "delhi",
    "new delhi",
    "ncr",
    "noida",
    "greater noida",
    "faridabad",
    "ghaziabad",
    "mumbai",
    "navi mumbai",
    "thane",
    "pune",
    "hyderabad",
    "secunderabad",
    "chennai",
    "kolkata",
    "ahmedabad",
    "gandhinagar",
    "jaipur",
    "chandigarh",
    "mohali",
    "panchkula",
    "kochi",
    "cochin",
    "trivandrum",
    "thiruvananthapuram",
    "coimbatore",
    "madurai",
    "indore",
    "bhopal",
    "nagpur",
    "nashik",
    "lucknow",
    "kanpur",
    "bhubaneswar",
    "mysore",
    "mysuru",
    "mangalore",
    "mangaluru",
    "hubli",
    "vadodara",
    "surat",
    "rajkot",
    "visakhapatnam",
    "vizag",
    "vijayawada",
    "dehradun",
    "patna",
    "ranchi",
    "guwahati",
    "raipur",
    "goa",
    "haryana",
    "karnataka",
    "maharashtra",
    "telangana",
    "tamil nadu",
    "kerala",
    "gujarat",
    "uttar pradesh",
    "west bengal",
    "rajasthan",
    "punjab",
    "odisha",
    "madhya pradesh",
    "andhra pradesh",
    "uttarakhand",
    "bihar",
    "jharkhand",
    "assam",
)

# A "remote" posting naming one of these (and not India) is not for India.
FOREIGN_MARKERS = (
    "us",
    "usa",
    "u.s.",
    "united states",
    "america",
    "americas",
    "north america",
    "latam",
    "canada",
    "mexico",
    "brazil",
    "uk",
    "united kingdom",
    "england",
    "london",
    "ireland",
    "europe",
    "eu",
    "emea",
    "germany",
    "france",
    "spain",
    "portugal",
    "netherlands",
    "poland",
    "switzerland",
    "sweden",
    "singapore",
    "australia",
    "new zealand",
    "japan",
    "philippines",
    "indonesia",
    "vietnam",
    "malaysia",
    "uae",
    "dubai",
    "israel",
    "south africa",
    "nigeria",
    "kenya",
    "est",
    "pst",
    "cet",
)

# Words that say nothing about the country: a location made only of these
# (e.g. "Hybrid", "Multiple locations") is kept, like an empty one.
GENERIC_LOCATION_WORDS = frozenset(
    {
        "remote",
        "hybrid",
        "onsite",
        "on-site",
        "on",
        "site",
        "in",
        "office",
        "work",
        "from",
        "home",
        "wfh",
        "anywhere",
        "flexible",
        "multiple",
        "various",
        "locations",
        "location",
        "or",
        "and",
        "first",
        "only",
        "global",
        "worldwide",
        "apac",
        "asia",
        "south",
        "time",
        "zone",
        "zones",
    }
)

_WORD_RE = re.compile(r"[a-z][a-z.\-]*")


def _has_term(text: str, terms: Iterable[str]) -> str:
    """First term found in ``text`` on word boundaries, else ''."""
    for term in terms:
        if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text):
            return term
    return ""


# Seniority/level words say nothing about the function: "Product Research
# Intern" is not a dev role. Ignored even if a configured keyword list has them.
LEVEL_WORDS = frozenset(
    {"intern", "interns", "internship", "trainee", "fresher", "graduate", "apprentice"}
)


def is_dev_role(role: str, skills: Iterable[str] = (), keywords: Iterable[str] = ()) -> bool:
    """True when the title (or extracted skills) names a developer-type role."""
    if _has_term(role.casefold(), NON_DEV_ROLE_TERMS):
        return False
    terms = tuple(k for k in keywords if k.casefold().strip() not in LEVEL_WORDS)
    haystack = " ".join([role, *skills]).casefold()
    return bool(_has_term(haystack, terms or DEFAULT_ROLE_KEYWORDS))


def location_ok(location: str, extra_terms: Iterable[str] = ()) -> tuple[bool, str]:
    """India location gate; returns (keep, reason)."""
    text = (location or "").casefold().strip()
    if not text:
        return True, "no location given"
    # Profile extras like "Remote" are not India places: they must not
    # vouch for "Remote - US".
    extras = tuple(
        t.casefold().strip()
        for t in extra_terms
        if t.strip() and t.casefold().strip() not in GENERIC_LOCATION_WORDS
    )
    india = _has_term(text, (*INDIA_TERMS, *extras))
    if india:
        return True, f"in India ({india})"
    foreign = _has_term(text, FOREIGN_MARKERS)
    if foreign:
        return False, f"outside India ({foreign})"
    words = set(_WORD_RE.findall(text))
    if words and words <= GENERIC_LOCATION_WORDS:
        return True, "remote/unspecified country"
    return False, f"location {location!r} not in India"


# --- Level gate: internships only --------------------------------------------

_INTERN_RE = re.compile(
    r"(?i)(?<![a-z])(intern|interns|internship|internships|apprentice|apprenticeship|"
    r"trainee|co-?op|summer\s+(?:analyst|associate|engineer))(?![a-z])"
)
# Explicitly experienced levels: never an internship even if "intern" appears
# in a long title (e.g. "Senior Engineer - Intern Programs").
_EXPERIENCED_RE = re.compile(
    r"(?i)(?<![a-z])(senior|sr\.?|staff|lead|principal|manager|architect|director|head|"
    r"vp|sde\s*-?\s*(?:ii|iii|iv|2|3|4)|swe\s*-?\s*(?:ii|iii|2|3)|"
    r"(?:engineer|developer)\s*(?:ii|iii|iv|2|3|4))(?![a-z0-9])"
)


def is_intern_role(role: str) -> bool:
    """True for internship / apprentice / trainee titles at entry level."""
    return bool(_INTERN_RE.search(role or "")) and not _EXPERIENCED_RE.search(role or "")


# --- Batch gate: graduation year ------------------------------------------------

_YEAR = r"20[2-3]\d"
_BATCH_PATTERNS = (
    # "2025 batch", "2025/2026 graduates", "2026 & 2027 passouts"
    re.compile(
        rf"(?i)((?:{_YEAR})(?:\s*(?:/|,|&|and|or|-|–)\s*(?:{_YEAR}))*)\s*"
        r"(?:batch|grads?\b|graduates?|graduating|pass\s*-?\s*outs?|passing\s+out|students)"
    ),
    # "batch of 2025", "graduating in 2026", "class of 2025, 2026", "passout year: 2025"
    re.compile(
        r"(?i)(?:batch|class|graduat\w*|pass\s*-?\s*out|passing\s+out)\s*"
        rf"(?:of|in|year|:|-|–)?\s*((?:{_YEAR})(?:\s*(?:/|,|&|and|or|-|–)\s*(?:{_YEAR}))*)"
    ),
)


def batch_years(text: str) -> set[int]:
    """Graduation years a posting explicitly targets (empty = unspecified)."""
    years: set[int] = set()
    for pattern in _BATCH_PATTERNS:
        for match in pattern.finditer(text or ""):
            years.update(int(y) for y in re.findall(_YEAR, match.group(1)))
    return years


def batch_ok(text: str, grad_year: int | None) -> tuple[bool, str]:
    """Drop postings that name eligible batches excluding ``grad_year``."""
    if not grad_year:
        return True, "no batch gate"
    years = batch_years(text)
    if not years or grad_year in years:
        return True, "batch ok"
    return False, f"batch {sorted(years)} excludes {grad_year}"


# --- Mail gate: job-alert mail vs. replies about your own applications -------

_NOT_ALERT_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # Replies about an application you already made.
        r"\bapplication\b.{0,40}\b(received|submitted|successful|confirm\w*|acknowledg\w*|"
        r"status|update|viewed|under review|pending|shortlisted|rejected|withdrawn|progress)",
        r"\b(received|reviewing|reviewed)\b.{0,25}\byour (application|resume|cv|profile)",
        r"\byour (application|candidature)\b",
        r"\bthank(s| you)\b.{0,15}\b(for )?(applying|your application|your interest)",
        r"\b(cv|resume|profile)\s*(was\s+|has been\s+)?(reviewed|viewed|downloaded|shortlisted)",
        r"\brecruiters? (viewed|saw|looked)",
        r"\bapplied\b.{0,30}\b(successfully|to|for)\b",
        r"\bfeedback on your\b",
        # Hiring-process mail, not new openings.
        r"\binterview\b.{0,30}\b(invit\w*|schedul\w*|confirm\w*|slot|round|call|"
        r"feedback|result|link|details)",
        r"\b(invit\w*|schedul\w*)\b.{0,30}\binterview",
        r"\b(online|coding|aptitude|technical)\s+(test|assessment|round|challenge)\b",
        r"\bassessment\b",
        r"\boffer letter\b",
        r"\bnext steps?\b",
        r"\b(regret|unfortunately|not (been )?selected|not moving forward|other candidates)\b",
        # Account / transactional / marketing noise.
        r"\b(otp|one[- ]time password|verification code|verify your|password reset|sign[- ]?in)\b",
        r"\b(receipt|invoice|payment|order|statement|bill)\b",
        r"\b(webinar|workshop|masterclass|course|certificate|tips to|how to crack)\b",
        r"\b(complete|update) your profile\b",
    )
)


def is_job_alert_mail(subject: str, sender: str = "") -> tuple[bool, str]:
    """False for replies about your own applications and other non-alert mail."""
    text = f"{subject}".strip()
    for pattern in _NOT_ALERT_PATTERNS:
        match = pattern.search(text)
        if match:
            return False, f"not a job alert ({match.group(0).strip()[:30]!r})"
    return True, "job alert"
