"""Fixed LLM pipeline: classify -> extract -> match -> suggest (Phase 4).

No agent framework — a plain deterministic sequence. Guardrails run BEFORE
any hosted LLM call (hidden-text strip, injection quarantine, PII redaction);
every LLM output is parsed into a Pydantic model with exactly one retry, then
the item is skipped safely. Scam-flagged postings and fabrication violations
never produce suggestions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import structlog
from pydantic import BaseModel
from sqlalchemy.orm import Session

from jobguard.agent.fallback import TokenBudget
from jobguard.agent.rank import Profile, score_posting
from jobguard.guardrails import (
    check_scam,
    collapse_boilerplate,
    redact_pii,
    scan_injection,
    strip_hidden_text,
)
from jobguard.llm.client import LLMClient, LLMError
from jobguard.llm.schemas import (
    Classification,
    MatchResult,
    PostingExtraction,
    SchemaValidationError,
    TailoringCard,
    parse_strict,
)
from jobguard.resume.checker import check_card
from jobguard.resume.loader import Resume
from jobguard.store.models import AuditLog, Posting, Suggestion

log = structlog.get_logger(__name__)

PipelineStatus = Literal["suggested", "skipped", "quarantined", "flagged", "violations"]

# Short example-based contracts. A full JSON-Schema dump backfires on some
# models (they echo the schema as content); a tiny example plus an explicit
# key list is followed reliably. Each example is valid per its Pydantic model.
_SYSTEMS: dict[str, str] = {
    "classify": (
        "Decide what kind of email this is. Respond with exactly one JSON "
        "object and no other text. The object must have exactly these keys: "
        "kind (one of: posting, newsletter, otp, spam, other), confidence "
        "(0 to 1), reason (string). "
        'Example: {"kind": "posting", "confidence": 0.9, '
        '"reason": "lists role and company"}'
    ),
    "extract": (
        "Extract the job posting fields. Respond with exactly one JSON "
        "object and no other text, with exactly these keys: company, role "
        "(both required non-empty strings), location, deadline, link, "
        "skills (array of strings), eligibility. Use empty strings/arrays "
        "when unknown. "
        'Example: {"company": "Acme", "role": "SDE Intern", "location": "Remote", '
        '"deadline": "", "link": "https://acme.example/j", '
        '"skills": ["Python"], "eligibility": "Final-year students"}'
    ),
    "match": (
        "Score this posting's fit 0-100. Respond with exactly one JSON "
        "object and no other text, with exactly these keys: score (0-100), "
        "reasons (array of strings), resume_item_ids (array of strings). "
        'Example: {"score": 82, "reasons": ["Python overlap"], "resume_item_ids": []}'
    ),
    "suggest": (
        "Suggest resume tailoring. Every suggestion must cite resume item IDs "
        "from the provided list, or be kind gap with no IDs. Respond with "
        "exactly one JSON object and no other text, with exactly these keys: "
        "posting_key, suggestions (array of {kind: reword|emphasize|gap, "
        "resume_item_ids, text}), gaps (array of strings). "
        'Example: {"posting_key": "k", "suggestions": '
        '[{"kind": "reword", "resume_item_ids": ["exp-1"], "text": "Emphasize FastAPI."}], '
        '"gaps": []}'
    ),
}


@dataclass
class PipelineResult:
    """Outcome for one posting (persisted to DB + audit log by the caller)."""

    status: PipelineStatus
    score: float = 0.0
    reasons: list[str] | None = None
    detail: str = ""


_RETRY_EXAMPLES: dict[str, str] = {
    "Classification": '{"kind": "posting", "confidence": 0.9, "reason": "..."}',
    "PostingExtraction": '{"company": "Acme", "role": "SDE Intern", "location": "", '
    '"deadline": "", "link": "", "skills": [], "eligibility": ""}',
    "MatchResult": '{"score": 0, "reasons": [], "resume_item_ids": []}',
    "TailoringCard": '{"posting_key": "k", "suggestions": [], "gaps": []}',
}


def _complete_parsed(
    client: LLMClient,
    model: type[BaseModel],
    *,
    system: str,
    user: str,
    budget: TokenBudget | None = None,
) -> BaseModel:
    """One LLM call with exactly one retry; raises on persistent failure.

    The retry repeats the shape contract with a minimal example: most
    validation failures are models punting with ``{}`` or echoing the
    schema, and restating the exact expected shape recovers them.
    """
    response = client.complete(system=system, user=user)
    if budget is not None:
        budget.charge(system=system, user=user, response=response)
    try:
        return parse_strict(model, response.text)
    except SchemaValidationError:
        example = _RETRY_EXAMPLES.get(model.__name__, "{}")
        retry_user = (
            user
            + "\n\nThat was not valid. Respond with exactly one JSON object "
            + f"and no other text, shaped exactly like this: {example}"
        )
        retry = client.complete(system=system, user=retry_user)
        if budget is not None:
            budget.charge(system=system, user=retry_user, response=retry)
        return parse_strict(model, retry.text)


def _audit(session: Session, event: str, detail: dict[str, Any]) -> None:
    session.add(AuditLog(event=event, detail=detail))


def run_pipeline(
    posting: Posting,
    *,
    body: str,
    sender: str,
    client: LLMClient,
    profile: Profile,
    resume: Resume,
    session: Session,
    budget: TokenBudget | None = None,
) -> PipelineResult:
    """Run the full pipeline for one stored posting. Never raises for content."""
    # --- Guardrails first: the LLM only sees cleaned, redacted text. ---
    hidden = strip_hidden_text(body)
    cleaned = collapse_boilerplate(hidden.text) or body
    if hidden.removed:
        _audit(
            session,
            "pipeline.hidden_text",
            {"posting_id": posting.id, "removed": len(hidden.removed)},
        )
    injection = scan_injection(cleaned)
    if injection.flagged:
        _audit(
            session,
            "pipeline.quarantine",
            {
                "posting_id": posting.id,
                "rules": sorted(f.rule for f in injection.findings),
            },
        )
        posting.pipe_status = "quarantined"
        session.commit()
        return PipelineResult(status="quarantined", detail="injection patterns matched")

    redacted = redact_pii(cleaned).text
    posting.pipe_status = "analyzing"
    posting.pipe_stage = "none"
    session.commit()

    # --- Step 1: classify. Non-postings stop here. ---
    try:
        classification = _complete_parsed(
            client,
            Classification,
            system=_SYSTEMS["classify"],
            user=f"[classify]\n{redacted}",
            budget=budget,
        )
    except (LLMError, SchemaValidationError, RuntimeError) as exc:
        _audit(session, "pipeline.classify_fail", {"posting_id": posting.id})
        posting.pipe_status = "failed"
        posting.pipe_stage = "none"
        session.commit()
        return PipelineResult(status="skipped", detail=f"classify failed: {type(exc).__name__}")
    assert isinstance(classification, Classification)
    posting.pipe_stage = "classified"
    session.commit()
    if classification.kind != "posting":
        _audit(
            session,
            "pipeline.skip_non_posting",
            {"posting_id": posting.id, "kind": classification.kind},
        )
        posting.pipe_status = "skipped_non_job"
        session.commit()
        return PipelineResult(status="skipped", detail=f"kind={classification.kind}")

    # --- Step 2: extract structured fields. ---
    try:
        extraction = _complete_parsed(
            client,
            PostingExtraction,
            system=_SYSTEMS["extract"],
            user=f"[extract]\n{redacted}",
            budget=budget,
        )
    except (LLMError, SchemaValidationError, RuntimeError) as exc:
        _audit(session, "pipeline.extract_fail", {"posting_id": posting.id})
        posting.pipe_status = "failed"
        session.commit()
        return PipelineResult(status="skipped", detail=f"extract failed: {type(exc).__name__}")
    assert isinstance(extraction, PostingExtraction)
    posting.pipe_stage = "extracted"
    session.commit()

    # --- Scam screen on extracted fields: flagged items stop here. ---
    scam = check_scam(
        company=extraction.company or posting.company,
        sender=sender,
        link=extraction.link or posting.link or "",
        text=redacted,
    )
    if scam.flagged:
        posting.scam_flag = True
        _audit(
            session,
            "pipeline.scam_flag",
            {"posting_id": posting.id, "signals": [s.signal for s in scam.signals]},
        )
        posting.pipe_status = "flagged_scam"
        session.commit()
        return PipelineResult(status="flagged", detail="scam heuristics matched")

    # --- Step 3: match against the profile. ---
    try:
        match = _complete_parsed(
            client,
            MatchResult,
            system=_SYSTEMS["match"],
            user=(
                "[match]\n"
                f"Profile skills: {', '.join(profile.skills)}\n"
                f"Posting: {extraction.model_dump_json()}"
            ),
            budget=budget,
        )
    except (LLMError, SchemaValidationError, RuntimeError) as exc:
        _audit(session, "pipeline.match_fail", {"posting_id": posting.id})
        posting.pipe_status = "failed"
        session.commit()
        return PipelineResult(status="skipped", detail=f"match failed: {type(exc).__name__}")
    assert isinstance(match, MatchResult)
    posting.pipe_stage = "matched"
    session.commit()

    heuristic = score_posting(
        profile,
        role=extraction.role,
        location=extraction.location,
        skills=extraction.skills,
        text=redacted,
    )
    reasons = list(match.reasons) + [
        f"heuristic rank {heuristic.score:.0f}: {'; '.join(heuristic.reasons) or 'no overlap'}"
    ]

    # --- Step 4: tailoring suggestions, fabrication-checked. ---
    try:
        card = _complete_parsed(
            client,
            TailoringCard,
            system=_SYSTEMS["suggest"],
            user=(
                "[suggest]\n"
                f"Resume IDs: {sorted(resume.known_ids())}\n"
                f"Posting: {extraction.model_dump_json()}"
            ),
            budget=budget,
        )
    except (LLMError, SchemaValidationError, RuntimeError) as exc:
        _audit(session, "pipeline.suggest_fail", {"posting_id": posting.id})
        posting.pipe_status = "failed"
        session.commit()
        return PipelineResult(status="skipped", detail=f"suggest failed: {type(exc).__name__}")
    assert isinstance(card, TailoringCard)

    violations = check_card(card, resume)
    if violations:
        _audit(
            session,
            "pipeline.fabrication_violation",
            {"posting_id": posting.id, "count": len(violations)},
        )
        posting.pipe_status = "violations"
        session.commit()
        return PipelineResult(status="violations", detail="; ".join(violations[:3]))

    posting.score = match.score
    posting.pipe_status = "done"
    posting.pipe_stage = "suggested"
    session.add(
        Suggestion(
            posting_id=posting.id,
            card=card.model_dump(),
            resume_item_ids=sorted({rid for s in card.suggestions for rid in s.resume_item_ids}),
            gaps=list(card.gaps),
        )
    )
    _audit(
        session,
        "pipeline.suggested",
        {"posting_id": posting.id, "score": match.score},
    )
    session.commit()
    return PipelineResult(status="suggested", score=match.score, reasons=reasons)
