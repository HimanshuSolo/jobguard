"""Scam heuristics for job postings (Phase 3).

Deterministic signals — fee requests, free-mail recruiters, urgency pressure,
and company/link-domain mismatch — each contribute to a score. At or above
``SCAM_THRESHOLD`` the posting is flagged: it goes to the digest's
"Flagged suspicious" section and is never recommended. An LLM judge (Phase 4)
adds a second opinion; heuristics alone already quarantine the obvious cases.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from pydantic import BaseModel, Field

SCAM_THRESHOLD = 2

_FEE_RE = re.compile(
    r"(?i)\b(registration fee|pay .{0,30}(to apply|for (training|kit|offer))|"
    r"refundable (deposit|fee|amount)|processing fee|investment required|"
    r"earn .{0,20}per day.{0,20}(whatsapp|telegram))\b"
)
_URGENCY_RE = re.compile(
    r"(?i)\b(act (now|fast|immediately)|last chance|limited (slots|offer|seats)|"
    r"apply within \d+ hours?|immediate(ly)? (required|needed)|urgent (opening|hiring))\b"
)
_FREE_MAIL_DOMAINS = ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "rediffmail.com")
_MESSENGER_RE = re.compile(r"(?i)\b(whatsapp|telegram)\b.{0,20}\b(\+?\d[\d\s\-]{7,}|contact)\b")


class ScamSignal(BaseModel):
    """One triggered heuristic with a short explanation."""

    signal: str
    detail: str


class ScamReport(BaseModel):
    """Heuristic verdict: ``flagged`` when score reaches the threshold."""

    flagged: bool = False
    score: int = 0
    signals: list[ScamSignal] = Field(default_factory=list)


def _domain_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host.removeprefix("www.")
    except ValueError:
        return ""


def check_scam(
    *,
    company: str,
    sender: str,
    link: str,
    text: str,
) -> ScamReport:
    """Score a posting against scam heuristics (all inputs untrusted)."""
    signals: list[ScamSignal] = []
    blob = f"{company}\n{sender}\n{link}\n{text}"

    if _FEE_RE.search(blob):
        signals.append(ScamSignal(signal="fee-request", detail="asks for payment/deposit to apply"))
    if _URGENCY_RE.search(blob):
        signals.append(
            ScamSignal(signal="urgency-pressure", detail="high-pressure urgency language")
        )
    sender_domain = ""
    if "@" in sender:
        # Trim envelope junk at the edges only; internal dots are significant.
        sender_domain = re.sub(r"^[<>\s]+|[<>\s.,;]+$", "", sender.rsplit("@", 1)[-1].lower())
    if sender_domain in _FREE_MAIL_DOMAINS and company:
        signals.append(
            ScamSignal(
                signal="free-mail-recruiter",
                detail=f"recruiter uses free mailbox {sender_domain}",
            )
        )
    if _MESSENGER_RE.search(blob):
        signals.append(
            ScamSignal(
                signal="messenger-contact",
                detail="pushes conversation to WhatsApp/Telegram with a number",
            )
        )
    link_domain = _domain_of(link)
    if company.strip() and link_domain:
        trusted_hosts = (
            "linkedin",
            "indeed",
            "naukri",
            "internshala",
            "unstop",
            "glassdoor",
            "greenhouse",
            "lever",
            "ashby",
            "workday",
            "smartrecruiters",
            "boards",
        )
        if not any(host in link_domain for host in trusted_hosts):
            stopwords = {
                "the",
                "co",
                "inc",
                "labs",
                "technologies",
                "technology",
                "systems",
                "solutions",
                "ltd",
                "pvt",
                "private",
                "limited",
            }
            words = [
                word
                for word in re.sub(r"[^a-z0-9 ]", " ", company.lower()).split()
                if word and word not in stopwords
            ]
            if words and not any(word in link_domain for word in words):
                signals.append(
                    ScamSignal(
                        signal="domain-mismatch",
                        detail=f"company {company!r} vs link domain {link_domain}",
                    )
                )
    score = len(signals)
    return ScamReport(flagged=score >= SCAM_THRESHOLD, score=score, signals=signals)
