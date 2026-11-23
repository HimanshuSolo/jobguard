"""JobGuard command line: init-db | run-worker | build-digest | eval | serve | seed-demo."""

from __future__ import annotations

import argparse
import sys

import structlog
from sqlalchemy.orm import Session, sessionmaker

from jobguard.agent import TokenBudget
from jobguard.agent.rank import Profile
from jobguard.config import Settings, get_settings
from jobguard.llm.client import LLMClient
from jobguard.resume import Resume

log = structlog.get_logger(__name__)


def _cmd_init_db(_: argparse.Namespace) -> int:
    from jobguard.store import get_engine, init_db

    settings = get_settings()
    engine = get_engine(settings.database_url)
    init_db(engine)
    print(f"Initialized database at {settings.database_url}")
    return 0


def _cmd_run_worker(_: argparse.Namespace) -> int:
    from jobguard.ingestion import sync_source
    from jobguard.sources.configure import build_adapters, build_prefilter
    from jobguard.store import get_engine, init_db, session_factory

    settings = get_settings()
    if settings.demo_mode:
        print("DEMO_MODE is on: run-worker does synthetic-only work; see seed-demo (Phase 6).")
        return 0

    engine = get_engine(settings.database_url)
    init_db(engine)
    prefilter = build_prefilter(settings)
    adapters, skipped = build_adapters(settings)
    for note in skipped:
        print(note)

    if not adapters:
        print("Nothing configured: add boards, RSS feeds, Gmail, or IMAP first.")
        return 0

    sessions = session_factory(engine)
    with sessions() as session:
        for adapter in adapters:
            result = sync_source(adapter, session, prefilter)
            print(
                f"{result.source}: fetched={result.fetched} new_raw={result.new_raw} "
                f"new={result.new_postings} merged={result.merged_postings} "
                f"filtered={result.filtered_out} seen={result.skipped_seen}"
            )
        _run_pipeline_stage(sessions, settings)
    return 0


def _run_pipeline_stage(sessions: sessionmaker[Session], settings: Settings) -> None:
    """Classify/extract/match/suggest for postings lacking suggestions."""
    from pathlib import Path

    from jobguard.agent import TokenBudget, load_profile
    from jobguard.ingestion import RETRYABLE_STATUSES, backfill_statuses, triage_posting
    from jobguard.llm.factory import build_pipeline_clients
    from jobguard.resume import load_resume
    from jobguard.store import Posting

    if settings.llm_provider.lower() == "fake":
        print("LLM provider is 'fake'; skipping pipeline stage (set LLM_PROVIDER).")
        return
    for path_attr, label in (("profile_path", "profile"), ("resume_path", "resume")):
        if not Path(str(getattr(settings, path_attr))).exists():
            print(f"No {label} at {getattr(settings, path_attr)}; skipping pipeline stage.")
            return
    try:
        client, fast_client, _limiter = build_pipeline_clients(settings)
    except (ValueError, RuntimeError) as exc:
        print(f"LLM client unavailable ({exc}); skipping pipeline stage.")
        return
    profile = load_profile(str(settings.profile_path))
    resume = load_resume(str(settings.resume_path))
    budget = TokenBudget(max_tokens=settings.llm_max_tokens_per_run)
    with sessions() as session:
        backfill_statuses(session)

        # Phase A: local triage for everything actionable (no LLM, no sleeping).
        # Terminal outcomes are persisted immediately and never re-processed.
        actionable = (
            session.query(Posting)
            .filter(
                Posting.pipe_status.in_(list(RETRYABLE_STATUSES) + ["analyzing"]),
                Posting.pipe_status.is_not(None),
            )
            .all()
        )
        triaged = {"terminal": 0, "queued": 0}
        for post in actionable:
            verdict = triage_posting(post, profile=profile, sender=post.sender or "")
            if verdict.status == "queued":
                post.cheap_score = verdict.cheap_score
                if post.pipe_status in ("analyzing", "failed"):
                    post.pipe_status = "queued"
                triaged["queued"] += 1
            else:
                post.pipe_status = verdict.status
                post.pipe_stage = "triaged"
                triaged["terminal"] += 1
        session.commit()
        print(f"triage: {triaged} (local only, no LLM calls, no sleeping)")

    # Phase B: LLM loop over the bounded selection (threshold + top-N).
    # Everything else stays queued with its cheap score, explicitly
    # "not analyzed yet", and can be analyzed on demand later.
    from jobguard.ingestion import select_for_llm

    with sessions() as session:
        queued = (
            session.query(Posting)
            .filter(Posting.pipe_status == "queued")
            .order_by(Posting.id)
            .all()
        )
        pending = select_for_llm(
            queued,
            top_n=settings.llm_top_n_per_run,
            min_cheap_score=settings.llm_cheap_score_min,
        )
        pending_ids = [p.id for p in pending]
        queued_count = len(queued)
    print(
        f"selected {len(pending_ids)}/{queued_count} for LLM "
        f"(top_n={settings.llm_top_n_per_run}, min_score={settings.llm_cheap_score_min})"
    )
    counts = _run_pool(
        session_factory=sessions,
        posting_ids=pending_ids,
        client=client,
        fast_client=fast_client,
        profile=profile,
        resume=resume,
        budget=budget,
        max_input_chars=settings.llm_max_input_chars,
        max_workers=settings.llm_workers,
    )
    print(f"pipeline: {counts or 'nothing pending'} (tokens used: {budget.used_tokens})")


def _run_pool(
    *,
    session_factory: sessionmaker[Session],
    posting_ids: list[int],
    client: LLMClient,
    fast_client: LLMClient,
    profile: Profile,
    resume: Resume,
    budget: TokenBudget,
    max_input_chars: int,
    max_workers: int,
) -> dict[str, int]:
    """Bounded worker pool: one DB session per item, shared throttled clients.

    Threads never share sessions or ORM objects (only ids cross the
    boundary); the limiter, budget, and circuit breaker are thread-safe.
    A BudgetExhausted run stops intake while in-flight items finish.
    """
    from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed

    from jobguard.agent import BudgetExhausted
    from jobguard.agent import run_pipeline as _run_pipeline
    from jobguard.store import Posting

    counts: dict[str, int] = {}
    stopped = False

    def _one(posting_id: int) -> str:
        with session_factory() as session:
            post = session.get(Posting, posting_id)
            if post is None or post.pipe_status != "queued":
                return "skipped-stale"
            try:
                result = _run_pipeline(
                    post,
                    body=post.body or "\n".join([post.role, post.company, post.location]),
                    sender=post.sender or "",
                    client=client,
                    profile=profile,
                    resume=resume,
                    session=session,
                    budget=budget,
                    max_input_chars=max_input_chars,
                    fast_client=fast_client,
                )
            except BudgetExhausted:
                return "budget-exhausted"
            return result.status

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {pool.submit(_one, pid): pid for pid in posting_ids}
        try:
            for future in as_completed(futures):
                try:
                    status = future.result()
                except CancelledError:
                    continue  # never started; item stays queued for next run
                if status == "budget-exhausted":
                    stopped = True
                    for other in futures:
                        other.cancel()
                    continue
                counts[status] = counts.get(status, 0) + 1
        finally:
            if stopped:
                for other in futures:
                    other.cancel()
    if stopped:
        print("Token budget exhausted; stopping pipeline stage.")
    return counts


def _cmd_build_digest(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from jobguard.digest import build_digest, record_sent, render_html, render_text
    from jobguard.notify import SendFailed, SendRefused, SmtpNotifier
    from jobguard.store import get_engine, init_db, session_factory

    settings = get_settings()
    engine = get_engine(settings.database_url)
    init_db(engine)
    sessions = session_factory(engine)
    with sessions() as session:
        digest = build_digest(
            session,
            kind=args.kind,
            best_score=settings.digest_best_score,
            urgent_hours=settings.digest_urgent_hours,
        )
        subject = digest.subject
        html_body = render_html(session, digest)
        text_body = render_text(session, digest)
        if args.dry_run:
            print(subject)
            print(text_body)
            return 0
        if digest.is_empty:
            print("Nothing new; skipping send (nothing recorded).")
            return 0
        if not settings.is_smtp_configured:
            print("SMTP not configured; digest built but not sent (nothing recorded).")
            print(text_body)
            return 0
        notifier = SmtpNotifier(
            host=settings.sender_smtp_host,
            port=settings.sender_smtp_port,
            user=settings.sender_smtp_user,
            password=settings.sender_smtp_password,
            from_address=settings.sender_from_address or settings.sender_smtp_user,
            owner_email=settings.owner_email,
            max_per_day=settings.sender_max_per_day,
        )
        try:
            notifier.send(
                session,
                to=settings.owner_email,
                subject=subject,
                html=html_body,
                text=text_body,
            )
        except (SendRefused, SendFailed) as exc:
            print(f"Digest not sent: {exc}")
            return 1
        digest_id = f"{args.kind}:{datetime.now(UTC).strftime('%Y-%m-%d')}"
        record_sent(session, digest_id, digest)
        print(f"Sent {args.kind} digest ({len(digest.tokens)} items) to {settings.owner_email}.")
    return 0


def _cmd_seed_demo(_: argparse.Namespace) -> int:
    from jobguard.store import get_engine, init_db, session_factory
    from jobguard.web import seed_demo

    settings = get_settings()
    if not settings.demo_mode:
        print("seed-demo refuses to run with DEMO_MODE off (it writes synthetic data).")
        return 1
    engine = get_engine(settings.database_url)
    init_db(engine)
    sessions = session_factory(engine)
    with sessions() as session:
        counts = seed_demo(session)
    print(f"Seeded demo data: {counts}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from jobguard.web import create_app

    settings = get_settings()
    app = create_app(settings)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _cmd_status(_: argparse.Namespace) -> int:
    from jobguard.ingestion import summarize
    from jobguard.store import get_engine, init_db, session_factory

    settings = get_settings()
    engine = get_engine(settings.database_url)
    init_db(engine)
    sessions = session_factory(engine)
    with sessions() as session:
        report = summarize(session)
    print(
        f"total={report['total']} scored={report['scored']} "
        f"eligible={report['eligible']} queued={report['queued']} "
        f"expired={report['expired']} suggestions={report['suggestions']}"
    )
    print("skipped_by_reason:")
    for reason, count in report["skipped_by_reason"].items():
        print(f"  {reason}: {count}")
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    # Local import: evals/ is top-level, not part of the package.
    # The CLI assumes it runs from the repo root (cases/baseline paths do too).
    import os
    from pathlib import Path

    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    from evals.runner import run_evals

    metrics = run_evals(
        cases_dir=args.cases_dir,
        baseline_path=args.baseline,
        out_path=args.out,
        owner_email=args.owner_email,
    )
    print(f"accuracy={metrics['accuracy']:.3f} passed={metrics['passed']}/{metrics['total']}")
    if args.report_html or args.report_md:
        from evals.report import to_html, to_markdown

        results = metrics.get("results", [])
        if args.report_html:
            Path(args.report_html).write_text(to_html(results, metrics), encoding="utf-8")
        if args.report_md:
            Path(args.report_md).write_text(to_markdown(results, metrics), encoding="utf-8")
    if args.live_smoke and not _live_smoke():
        return 1
    if metrics.get("regressions"):
        print("REGRESSIONS:", file=sys.stderr)
        for reg in metrics["regressions"]:
            print(f"  - {reg}", file=sys.stderr)
        return 1
    return 0


def _live_smoke() -> bool:
    """One synthetic classification through the configured provider.

    Nightly drift check: proves the provider is reachable and still emits
    schema-valid output. No real data; never fails silently into a digest.
    """
    from jobguard.llm.factory import build_client
    from jobguard.llm.schemas import Classification, parse_strict

    try:
        settings = get_settings()
        if settings.llm_provider.lower() == "fake":
            # No live surface with the Fake client; the deterministic suite
            # already covers this path. Nightly sets a real provider.
            print("live-smoke skipped: LLM_PROVIDER is 'fake'")
            return True
        client = build_client(settings)
        response = client.complete(
            system="Classify job-alert text. Respond with JSON only.",
            user="[classify]\nSDE Intern at Acme Labs. Apply at https://acme.example/j",
        )
        parsed = parse_strict(Classification, response.text)
        assert isinstance(parsed, Classification)
        print(f"live-smoke ok: {client.name} kind={parsed.kind}")
        return True
    except Exception as exc:
        print(f"live-smoke FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def _stub(name: str, phase: int) -> int:
    print(f"STUB (Phase {phase}): `jobguard {name}` is not implemented yet.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobguard", description="JobGuard agent CLI.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create database tables.")

    p_eval = sub.add_parser("eval", help="Run the eval suite and compare to baseline.")
    p_eval.add_argument("--cases-dir", default="data/evals")
    p_eval.add_argument("--baseline", default="evals/baseline.json")
    p_eval.add_argument("--out", default="evals/metrics.json")
    p_eval.add_argument("--owner-email", default="owner@example.com")
    p_eval.add_argument("--report-html", default="", help="Write an HTML report (Pages).")
    p_eval.add_argument("--report-md", default="", help="Write a Markdown table (PR comments).")
    p_eval.add_argument(
        "--live-smoke",
        action="store_true",
        help="One synthetic call through the configured live LLM (nightly drift check).",
    )
    p_eval.set_defaults(func=_cmd_eval)

    p_worker = sub.add_parser("run-worker", help="Sync all configured sources into the DB.")
    p_worker.set_defaults(func=_cmd_run_worker)

    p_status = sub.add_parser("status", help="Honest pipeline progress report.")
    p_status.set_defaults(func=_cmd_status)

    p_digest = sub.add_parser("build-digest", help="Build and send a digest email.")
    p_digest.add_argument("--kind", choices=["morning", "evening", "urgent"], default="morning")
    p_digest.add_argument("--dry-run", action="store_true", help="Print, do not send or record.")
    p_digest.set_defaults(func=_cmd_build_digest)

    p_serve = sub.add_parser("serve", help="Run the dashboard web server.")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=_cmd_serve)

    p_seed = sub.add_parser("seed-demo", help="Load synthetic demo data (DEMO_MODE only).")
    p_seed.set_defaults(func=_cmd_seed_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (``jobguard`` script)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:  # init-db has no custom func attribute
        return _cmd_init_db(args)
    return int(func(args))


if __name__ == "__main__":
    raise SystemExit(main())
