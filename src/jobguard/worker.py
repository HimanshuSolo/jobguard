"""Local (LLM-free) worker stages shared by the CLI and the dashboard."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from jobguard.agent.rank import Profile, load_profile
from jobguard.config import Settings


def triage_stage(sessions: sessionmaker[Session], settings: Settings) -> Profile:
    """Re-apply relevance gates to every stored job, then cheap-score the queue.

    The relevance re-check covers already-analyzed rows too, so tightening
    the rules (interns only, India only, your batch) also removes old rows,
    e.g. an "SDE 3" that was AI-scored before the intern gate existed. Needs
    no LLM and no resume.
    """
    from jobguard.ingestion import (
        RETRYABLE_STATUSES,
        NormalizedPosting,
        backfill_statuses,
        keep,
        triage_posting,
    )
    from jobguard.sources.configure import build_prefilter
    from jobguard.store import Posting

    profile = (
        load_profile(str(settings.profile_path))
        if Path(str(settings.profile_path)).exists()
        else Profile()
    )
    prefilter = build_prefilter(settings)
    counts = {"terminal": 0, "queued": 0, "filtered": 0}
    with sessions() as session:
        backfill_statuses(session)
        rows = session.query(Posting).filter(Posting.pipe_status != "filtered").all()
        for post in rows:
            relevant, _reason = keep(
                NormalizedPosting(
                    company=post.company or "",
                    role=post.role or "",
                    location=post.location or "",
                    skills=list(post.skills or []),
                    source=post.source or "",
                    eligibility=post.eligibility or "",
                    body=post.body or "",
                ),
                post.sender or "",
                prefilter,
            )
            if not relevant:
                post.pipe_status = "filtered"
                post.pipe_stage = "triaged"
                counts["filtered"] += 1
                continue
            if post.pipe_status not in (*RETRYABLE_STATUSES, "analyzing"):
                continue
            verdict = triage_posting(post, profile=profile, sender=post.sender or "")
            if verdict.status == "queued":
                post.cheap_score = verdict.cheap_score
                if post.pipe_status in ("analyzing", "failed"):
                    post.pipe_status = "queued"
                counts["queued"] += 1
            else:
                post.pipe_status = verdict.status
                post.pipe_stage = "triaged"
                counts["terminal"] += 1
        session.commit()
    print(f"triage: {counts} (local only, no LLM calls, no sleeping)")
    return profile
