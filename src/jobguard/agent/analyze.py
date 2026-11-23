"""On-demand analysis of a single posting (Phase 9, Step 2.9).

Queued items are "not analyzed yet" until the bounded worker reaches them;
this runs the same local-triage-then-LLM path for one posting right away,
from the ``jobguard analyze`` CLI command or the dashboard Analyze button.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from jobguard.agent import TokenBudget, load_profile, run_pipeline
from jobguard.config import Settings
from jobguard.ingestion import triage_posting
from jobguard.llm.client import LLMClient, LLMResponse
from jobguard.llm.factory import build_pipeline_clients
from jobguard.resume import load_resume
from jobguard.store import Posting


@dataclass
class AnalyzeResult:
    """Outcome for one posting: what happened and whether any LLM ran."""

    posting_id: int
    outcome: str  # analyzed | already | refused | not-found
    status: str  # pipe_status after the call ("" when the row is missing)
    detail: str = ""
    llm_calls: int = 0


class _CountingClient:
    """Pass-through wrapper that counts complete() calls (no behavior change)."""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.calls = 0

    @property
    def name(self) -> str:
        return self._inner.name

    def complete(self, *, system: str, user: str) -> LLMResponse:
        self.calls += 1
        return self._inner.complete(system=system, user=user)


def analyze_posting(
    posting_id: int,
    *,
    sessions: sessionmaker[Session],
    settings: Settings,
    force: bool = False,
) -> AnalyzeResult:
    """Triage one posting locally, then run the LLM path unless refused.

    Terminal postings are left alone (reported as ``already``) unless
    ``force`` re-runs them. Expired/quarantined/scam verdicts never reach
    the LLM. Returns the outcome without raising for content problems.
    """
    if settings.llm_provider.lower() == "fake" and not force:
        return AnalyzeResult(posting_id, "refused", "", "LLM provider is 'fake'; set LLM_PROVIDER.")
    for path_attr, label in (("profile_path", "profile"), ("resume_path", "resume")):
        if not Path(str(getattr(settings, path_attr))).exists():
            return AnalyzeResult(posting_id, "refused", "", f"No {label} file found.")
    try:
        client, fast_client, _limiter = build_pipeline_clients(settings)
    except (ValueError, RuntimeError) as exc:
        return AnalyzeResult(posting_id, "refused", "", f"LLM client unavailable: {exc}")
    profile = load_profile(str(settings.profile_path))
    resume = load_resume(str(settings.resume_path))
    budget = TokenBudget(max_tokens=settings.llm_max_tokens_per_run)

    with sessions() as session:
        post = session.get(Posting, posting_id)
        if post is None:
            return AnalyzeResult(posting_id, "not-found", "", "No such posting.")
        verdict = triage_posting(post, profile=profile, sender=post.sender or "")
        if verdict.status == "queued":
            post.cheap_score = verdict.cheap_score
            if post.pipe_status in ("analyzing", "failed"):
                post.pipe_status = "queued"
            session.commit()
        elif not force:
            post.pipe_status = verdict.status
            post.pipe_stage = "triaged"
            session.commit()
            return AnalyzeResult(
                posting_id,
                "already",
                verdict.status,
                f"Terminal ({verdict.status}); use --force to re-run.",
            )
        counting = _CountingClient(client)
        fast_counting = _CountingClient(fast_client)
        try:
            result = run_pipeline(
                post,
                body=post.body or "\n".join([post.role, post.company, post.location]),
                sender=post.sender or "",
                client=counting,
                fast_client=fast_counting,
                profile=profile,
                resume=resume,
                session=session,
                budget=budget,
                max_input_chars=settings.llm_max_input_chars,
            )
        except Exception as exc:  # never raise for content/infra problems
            session.rollback()
            return AnalyzeResult(posting_id, "refused", "", f"Pipeline error: {exc}")
        return AnalyzeResult(
            posting_id,
            "analyzed",
            result.status,
            result.detail or f"score={result.score:.0f}",
            llm_calls=counting.calls + fast_counting.calls,
        )
