"""Fixed LLM pipeline: classify -> extract -> match -> suggest (Phase 4).

No agent framework — a plain deterministic sequence. Guardrails run BEFORE
any hosted LLM call (hidden-text strip, injection quarantine, PII redaction);
every LLM output is parsed into a Pydantic model with exactly one retry, then
the item is skipped safely. Scam-flagged postings and fabrication violations
never produce suggestions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from jobguard.agent.fallback import BudgetExhausted, TokenBudget
from jobguard.agent.meter import RunMeter
from jobguard.agent.rank import Profile, score_posting
from jobguard.guardrails import (
    check_scam,
    collapse_boilerplate,
    redact_pii,
    scan_injection,
    strip_hidden_text,
)
from jobguard.llm.cache import cache_get, cache_key, cache_put
from jobguard.llm.client import LLMClient, LLMError
from jobguard.llm.schemas import (
    ClassifyExtract,
    MatchAndSuggest,
    PostingExtraction,
    SchemaValidationError,
    parse_strict,
)
from jobguard.resume.checker import check_card
from jobguard.resume.loader import Resume
from jobguard.resume.summary import compact_resume
from jobguard.store.models import AuditLog, Posting, Suggestion

log = structlog.get_logger(__name__)

PipelineStatus = Literal["suggested", "skipped", "quarantined", "flagged", "violations"]

# Short example-based contracts. A full JSON-Schema dump backfires on some
# models (they echo the schema as content); a tiny example plus an explicit
# key list is followed reliably. Each example is valid per its Pydantic model.
_SYSTEMS: dict[str, str] = {
    "classify_extract": (
        "Decide what kind of email this is AND extract the job posting "
        "fields, in one JSON object and no other text. The object must have "
        "exactly these keys: kind (one of: posting, newsletter, otp, spam, "
        "other), confidence (0 to 1), reason (string), company, role, "
        "location, deadline, link, skills (array of strings), eligibility. "
        "When kind is not posting, leave company/role/location/deadline/link "
        "empty. When kind is posting, company and role are required "
        "non-empty strings; use empty strings/arrays for unknown fields. "
        'Example: {"kind": "posting", "confidence": 0.9, '
        '"reason": "lists role and company", '
        '"company": "Acme", "role": "SDE Intern", "location": "Remote", '
        '"deadline": "", "link": "https://acme.example/j", '
        '"skills": ["Python"], "eligibility": "Final-year students"}'
    ),
    "match_suggest": (
        "Score this posting's fit 0-100 AND suggest resume tailoring, in one "
        "JSON object and no other text, with exactly these keys: score (0-100), "
        "reasons (array of strings), resume_item_ids (array of strings), "
        "posting_key (string), suggestions (array of {kind: reword|emphasize|gap, "
        "resume_item_ids, text}), gaps (array of strings). Every suggestion must "
        "cite resume item IDs from the provided list, or be kind gap with no IDs. "
        'Example: {"score": 82, "reasons": ["Python overlap"], "resume_item_ids": ["exp-1"], '
        '"posting_key": "k", "suggestions": [{"kind": "reword", "resume_item_ids": ["exp-1"], '
        '"text": "Emphasize FastAPI."}], "gaps": []}'
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
    "ClassifyExtract": '{"kind": "posting", "confidence": 0.9, "reason": "...", '
    '"company": "Acme", "role": "SDE Intern", "location": "", '
    '"deadline": "", "link": "", "skills": [], "eligibility": ""}',
    "MatchAndSuggest": '{"score": 0, "reasons": [], "resume_item_ids": [], '
    '"posting_key": "k", "suggestions": [], "gaps": []}',
}

# Bump when any prompt in _SYSTEMS changes shape or meaning: cached
# responses keyed on an older version are never served.
PROMPT_VERSION = "v2"


def _complete_parsed(
    client: LLMClient,
    model: type[BaseModel],
    *,
    system: str,
    user: str,
    session: Session,
    posting_id: int,
    stage: str,
    budget: TokenBudget | None = None,
    use_cache: bool = True,
    meter: RunMeter | None = None,
) -> BaseModel:
    """One LLM call with exactly one retry; raises on persistent failure.

    The retry repeats the shape contract with a minimal example: most
    validation failures are models punting with ``{}`` or echoing the
    schema, and restating the exact expected shape recovers them.

    With ``use_cache``, a stored response for this exact (model family,
    prompt version, content) is served with zero network calls and zero
    budget charge; only successfully parsed responses are stored.
    """
    key = cache_key(client.name, PROMPT_VERSION, user)
    if use_cache:
        hit = cache_get(session, key)
        if hit is not None:
            try:
                parsed_hit = parse_strict(model, hit)
            except SchemaValidationError:
                pass  # stale entry shape; fall through to a live call
            else:
                _audit(
                    session,
                    "pipeline.cache_hit",
                    {"posting_id": posting_id, "stage": stage},
                )
                if meter is not None:
                    meter.record_hit(stage)
                return parsed_hit
    start = time.perf_counter()
    response = client.complete(system=system, user=user)
    if meter is not None:
        meter.record_call(
            stage,
            system=system,
            user=user,
            text=response.text,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_s=time.perf_counter() - start,
        )
    if budget is not None:
        budget.charge(system=system, user=user, response=response)
    try:
        parsed = parse_strict(model, response.text)
        valid_text = response.text
    except SchemaValidationError:
        example = _RETRY_EXAMPLES.get(model.__name__, "{}")
        retry_user = (
            user
            + "\n\nThat was not valid. Respond with exactly one JSON object "
            + f"and no other text, shaped exactly like this: {example}"
        )
        start = time.perf_counter()
        retry = client.complete(system=system, user=retry_user)
        if meter is not None:
            meter.record_call(
                stage,
                system=system,
                user=retry_user,
                text=retry.text,
                input_tokens=retry.input_tokens,
                output_tokens=retry.output_tokens,
                latency_s=time.perf_counter() - start,
            )
        if budget is not None:
            budget.charge(system=system, user=retry_user, response=retry)
        parsed = parse_strict(model, retry.text)
        valid_text = retry.text
    if use_cache:
        cache_put(
            session,
            key=key,
            model_family=client.name,
            prompt_version=PROMPT_VERSION,
            response_text=valid_text,
        )
    return parsed


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
    max_input_chars: int = 3000,
    fast_client: LLMClient | None = None,
    use_cache: bool = True,
    meter: RunMeter | None = None,
) -> PipelineResult:
    """Run the full pipeline for one stored posting. Never raises for content."""
    # --- Guardrails first: the LLM only sees cleaned, redacted text. ---
    hidden = strip_hidden_text(body)
    cleaned = collapse_boilerplate(hidden.text) or body
    if len(cleaned) > max_input_chars:
        cleaned = cleaned[:max_input_chars] + "\n…[input truncated]"
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
    if len(redacted) > max_input_chars:
        redacted = redacted[:max_input_chars] + "\n…[input truncated]"
    # Cheap steps run on the fast model when one is configured; the combined
    # match+suggest step always uses the smart client. Defaults to one client.
    cheap_client = fast_client or client
    posting.pipe_status = "analyzing"
    posting.pipe_stage = "none"
    session.commit()

    # --- Steps 1+2: classify + extract, skipped when template parsers have
    # already produced labeled company/role/link. Remaining steps use the
    # fast client when one is configured.
    template_parsed = bool(
        (posting.company or "").strip()
        and (posting.role or "").strip()
        and (posting.link or "").strip()
    )
    if template_parsed:
        extraction = PostingExtraction(
            company=posting.company,
            role=posting.role,
            location=posting.location or "",
            link=posting.link or "",
            skills=list(posting.skills or []),
            eligibility=posting.eligibility or "",
        )
        posting.pipe_stage = "extracted"
        session.commit()
    else:
        try:
            merged = _complete_parsed(
                cheap_client,
                ClassifyExtract,
                system=_SYSTEMS["classify_extract"],
                user=f"[classify_extract]\n{redacted}",
                session=session,
                posting_id=posting.id,
                stage="classify_extract",
                budget=budget,
                use_cache=use_cache,
                meter=meter,
            )
        except BudgetExhausted:
            raise
        except (LLMError, SchemaValidationError, RuntimeError) as exc:
            _audit(session, "pipeline.classify_extract_fail", {"posting_id": posting.id})
            posting.pipe_status = "failed"
            posting.pipe_stage = "none"
            session.commit()
            return PipelineResult(
                status="skipped", detail=f"classify+extract failed: {type(exc).__name__}"
            )
        assert isinstance(merged, ClassifyExtract)
        posting.pipe_stage = "classified"
        session.commit()
        if merged.kind != "posting":
            _audit(
                session,
                "pipeline.skip_non_posting",
                {"posting_id": posting.id, "kind": merged.kind},
            )
            posting.pipe_status = "skipped_non_job"
            session.commit()
            return PipelineResult(status="skipped", detail=f"kind={merged.kind}")
        try:
            extraction = PostingExtraction.model_validate(merged.model_dump())
        except ValidationError as exc:
            _audit(session, "pipeline.extract_fail", {"posting_id": posting.id})
            posting.pipe_status = "failed"
            session.commit()
            return PipelineResult(status="skipped", detail=f"extract failed: {type(exc).__name__}")
        posting.pipe_stage = "extracted"
        session.commit()
    assert extraction.company and extraction.role

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

    # --- Steps 3+4: match + suggest in ONE call. The flat MatchAndSuggest
    # schema validates exactly as strictly as the separate schemas did.
    try:
        combo = _complete_parsed(
            client,
            MatchAndSuggest,
            system=_SYSTEMS["match_suggest"],
            user=(
                "[match_suggest]\n"
                f"Profile skills: {', '.join(profile.skills)}\n"
                f"Resume:\n{compact_resume(resume)}\n"
                f"Posting: {extraction.model_dump_json()}"
            ),
            session=session,
            posting_id=posting.id,
            stage="match_suggest",
            budget=budget,
            use_cache=use_cache,
            meter=meter,
        )
    except BudgetExhausted:
        raise
    except (LLMError, SchemaValidationError, RuntimeError) as exc:
        _audit(session, "pipeline.match_fail", {"posting_id": posting.id})
        posting.pipe_status = "failed"
        session.commit()
        return PipelineResult(
            status="skipped", detail=f"match+suggest failed: {type(exc).__name__}"
        )
    assert isinstance(combo, MatchAndSuggest)
    posting.pipe_stage = "matched"
    session.commit()

    heuristic = score_posting(
        profile,
        role=extraction.role,
        location=extraction.location,
        skills=extraction.skills,
        text=redacted,
    )
    reasons = list(combo.reasons) + [
        f"heuristic rank {heuristic.score:.0f}: {'; '.join(heuristic.reasons) or 'no overlap'}"
    ]

    card = combo.as_tailoring_card()
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

    posting.score = combo.score
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
        {"posting_id": posting.id, "score": combo.score},
    )
    session.commit()
    return PipelineResult(status="suggested", score=combo.score, reasons=reasons)
