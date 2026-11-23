"""Minimal eval runner (Phase 1).

Loads every ``*.jsonl`` case file under the cases directory, dispatches each
case by ``task``, and writes ``metrics.json``. Compares against
``baseline.json``; the returned metrics dict carries a ``regressions`` list
(non-empty means CI must fail).

Supported tasks in Phase 1 (later phases register more, no runner rewrite):
- ``policy_decision``: run the deterministic policy registry, compare verdict.
- ``schema_parse``: validate a payload against an ``jobguard.llm.schemas``
  model; ``expected.valid`` says whether it must pass.
- ``replay_determinism``: record one Fake call to a temp JSONL file, replay
  it, and require identical text.
- ``demo_mode_guard``: ``require_live_credentials`` must raise (or not).

``{{OWNER_EMAIL}}`` inside case inputs is substituted with the runner's
``owner_email`` so cases stay synthetic and portable.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from evals.metrics import compare_with_baseline, summarize

_VALID_TASKS = (
    "policy_decision",
    "schema_parse",
    "replay_determinism",
    "demo_mode_guard",
    "canonical_equal",
    "merge_triple",
    "prefilter_keep",
    "email_parser",
    "ssrf_guard",
    "normalize_board",
    "sync_idempotent",
    "hidden_text",
    "injection_scan",
    "pii_redact",
    "scam_check",
    "link_check",
    "pipeline_smoke",
    "rank_order",
    "fabrication",
    "digest_build",
    "send_policy",
    "outbound_scan",
    "approval_flow",
    "digest_split",
)


def _substitute(value: Any, owner_email: str) -> Any:
    if isinstance(value, str):
        return value.replace("{{OWNER_EMAIL}}", owner_email)
    if isinstance(value, dict):
        return {k: _substitute(v, owner_email) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, owner_email) for v in value]
    return value


def load_cases(cases_dir: str | Path) -> list[dict[str, Any]]:
    """Load all JSONL cases from ``cases_dir`` (sorted by filename)."""
    cases: list[dict[str, Any]] = []
    root = Path(cases_dir)
    for path in sorted(root.glob("*.jsonl")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            case = json.loads(line)
            case["_file"] = f"{path.name}:{lineno}"
            cases.append(case)
    return cases


def run_case(case: dict[str, Any], owner_email: str) -> dict[str, Any]:
    """Execute one case; never raises — failures are recorded, not thrown."""
    case_id = str(case.get("id", "?"))
    category = str(case.get("category", "uncategorized"))
    task = str(case.get("task", ""))
    try:
        if task == "policy_decision":
            return _run_policy(case, owner_email)
        if task == "schema_parse":
            return _run_schema(case)
        if task == "replay_determinism":
            return _run_replay(case)
        if task == "demo_mode_guard":
            return _run_demo_guard(case)
        if task == "canonical_equal":
            return _run_canonical_equal(case)
        if task == "merge_triple":
            return _run_merge_triple(case)
        if task == "prefilter_keep":
            return _run_prefilter_keep(case)
        if task == "email_parser":
            return _run_email_parser(case)
        if task == "ssrf_guard":
            return _run_ssrf_guard(case)
        if task == "normalize_board":
            return _run_normalize_board(case)
        if task == "sync_idempotent":
            return _run_sync_idempotent(case)
        if task == "hidden_text":
            return _run_hidden_text(case)
        if task == "injection_scan":
            return _run_injection_scan(case)
        if task == "pii_redact":
            return _run_pii_redact(case)
        if task == "scam_check":
            return _run_scam_check(case)
        if task == "link_check":
            return _run_link_check(case)
        if task == "pipeline_smoke":
            return _run_pipeline_smoke(case)
        if task == "rank_order":
            return _run_rank_order(case)
        if task == "fabrication":
            return _run_fabrication(case)
        if task == "digest_build":
            return _run_digest_build(case)
        if task == "send_policy":
            return _run_send_policy(case, owner_email)
        if task == "outbound_scan":
            return _run_outbound_scan(case)
        if task == "approval_flow":
            return _run_approval_flow(case)
        if task == "digest_split":
            return _run_digest_split(case)
        return {
            "id": case_id,
            "category": category,
            "task": task,
            "passed": False,
            "detail": f"unknown task {task!r}",
        }
    except Exception as exc:  # fail-safe: one bad case never kills the run
        return {
            "id": case_id,
            "category": category,
            "task": task,
            "passed": False,
            "detail": f"runner error: {exc}",
        }


def _run_policy(case: dict[str, Any], owner_email: str) -> dict[str, Any]:
    from jobguard.policy import PolicyContext, evaluate

    raw = _substitute(dict(case.get("input", {})), owner_email)
    expected = str(case.get("expected", {}).get("decision", ""))
    verdict = evaluate(PolicyContext(**raw))
    passed = verdict.decision == expected
    return {
        "id": case.get("id"),
        "category": case.get("category"),
        "task": case.get("task"),
        "passed": passed,
        "detail": f"got {verdict.decision}, want {expected}: {verdict.reason}",
    }


def _run_schema(case: dict[str, Any]) -> dict[str, Any]:
    from pydantic import ValidationError

    import jobguard.llm.schemas as schemas

    schema_name = str(case.get("input", {}).get("schema", ""))
    payload = case.get("input", {}).get("payload", {})
    expected_valid = bool(case.get("expected", {}).get("valid", True))
    model = getattr(schemas, schema_name, None)
    if model is None:
        return {
            "id": case.get("id"),
            "category": case.get("category"),
            "task": case.get("task"),
            "passed": False,
            "detail": f"unknown schema {schema_name!r}",
        }
    try:
        model.model_validate(payload)
        valid = True
        detail = "parsed ok"
    except ValidationError as exc:
        valid = False
        detail = f"rejected: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}"
    return {
        "id": case.get("id"),
        "category": case.get("category"),
        "task": case.get("task"),
        "passed": valid == expected_valid,
        "detail": f"{detail} (want valid={expected_valid})",
    }


def _run_replay(case: dict[str, Any]) -> dict[str, Any]:
    from jobguard.llm.fake import FakeClient
    from jobguard.llm.replay import RecordReplayClient

    system = str(case.get("input", {}).get("system", ""))
    user = str(case.get("input", {}).get("user", ""))
    response = str(case.get("input", {}).get("response", ""))
    want = str(case.get("expected", {}).get("replayed_text", ""))
    with tempfile.TemporaryDirectory() as tmp:
        record_path = Path(tmp) / "replay.jsonl"
        recorder = RecordReplayClient(FakeClient(default=response), record_path, mode="record")
        first = recorder.complete(system=system, user=user)
        replayer = RecordReplayClient(
            FakeClient(default="SHOULD-NOT-BE-USED"), record_path, mode="replay"
        )
        second = replayer.complete(system=system, user=user)
    passed = first.text == second.text == want
    return {
        "id": case.get("id"),
        "category": case.get("category"),
        "task": case.get("task"),
        "passed": passed,
        "detail": f"recorded==replayed==expected: {passed}",
    }


def _run_demo_guard(case: dict[str, Any]) -> dict[str, Any]:
    from jobguard.config import DemoModeError, Settings

    demo_mode = bool(case.get("input", {}).get("demo_mode", True))
    want_raise = bool(case.get("expected", {}).get("raises", False))
    settings = Settings(demo_mode=demo_mode)
    try:
        settings.require_live_credentials()
        raised = ""
    except DemoModeError:
        raised = "DemoModeError"
    passed = bool(raised) == want_raise
    return {
        "id": case.get("id"),
        "category": case.get("category"),
        "task": case.get("task"),
        "passed": passed,
        "detail": f"raised={raised!r} want_raise={want_raise}",
    }


def _result(case: dict[str, Any], passed: bool, detail: str) -> dict[str, Any]:
    return {
        "id": case.get("id"),
        "category": case.get("category"),
        "task": case.get("task"),
        "passed": passed,
        "detail": detail,
    }


def _run_canonical_equal(case: dict[str, Any]) -> dict[str, Any]:
    from jobguard.ingestion import canonical_key

    a = case.get("input", {}).get("a", {})
    b = case.get("input", {}).get("b", {})
    want = bool(case.get("expected", {}).get("equal", True))
    got = canonical_key(
        a.get("company", ""), a.get("role", ""), a.get("location", ""), a.get("link", "")
    ) == canonical_key(
        b.get("company", ""), b.get("role", ""), b.get("location", ""), b.get("link", "")
    )
    return _result(case, got == want, f"equal={got} want={want}")


def _run_merge_triple(case: dict[str, Any]) -> dict[str, Any]:
    from jobguard.ingestion import NormalizedPosting, merge_duplicates

    posts = [NormalizedPosting(**p) for p in case.get("input", {}).get("posts", [])]
    merged = merge_duplicates(posts)
    want_count = int(case.get("expected", {}).get("count", 1))
    want_seen = sorted(case.get("expected", {}).get("seen_on", []))
    got_seen = sorted(merged[0].seen_on) if merged else []
    passed = len(merged) == want_count and got_seen == want_seen
    return _result(case, passed, f"count={len(merged)} seen_on={got_seen}")


def _run_prefilter_keep(case: dict[str, Any]) -> dict[str, Any]:
    from jobguard.ingestion import NormalizedPosting, Prefilter, keep

    raw = case.get("input", {})
    post = NormalizedPosting(**raw.get("post", {}))
    if "source" in raw:
        post.source = str(raw["source"])
    prefilter = Prefilter.from_settings(raw.get("keywords_csv", ""), raw.get("senders_csv", ""))
    kept, reason = keep(post, str(raw.get("sender", "")), prefilter)
    want = bool(case.get("expected", {}).get("keep", True))
    return _result(case, kept == want, f"keep={kept} ({reason}) want={want}")


def _run_email_parser(case: dict[str, Any]) -> dict[str, Any]:
    from jobguard.sources import parse_alert

    raw = case.get("input", {})
    parsed = parse_alert(
        str(raw.get("sender", "")), str(raw.get("subject", "")), str(raw.get("body", ""))
    )
    want = case.get("expected", {})
    if parsed is None:
        return _result(case, False, "parser returned None")
    passed = (
        parsed.platform == want.get("platform")
        and parsed.company == want.get("company")
        and parsed.role == want.get("role")
    )
    return _result(case, passed, f"got {parsed.platform}/{parsed.company}/{parsed.role}")


def _run_ssrf_guard(case: dict[str, Any]) -> dict[str, Any]:
    import httpx

    from jobguard.sources import (
        FetchBlockedIp,
        FetchDenied,
        FetchFailed,
        FetchTooLarge,
        fetch_url_safe,
    )

    raw = case.get("input", {})
    status = int(raw.get("mock_status", 200))
    body = str(raw.get("mock_body", "")).encode()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body)

    outcome = "ok"
    try:
        fetch_url_safe(
            str(raw.get("url", "")),
            allowed_domains=list(raw.get("allowed_domains", [])),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            resolve=lambda host: [str(raw.get("resolve_ip", ""))],
        )
    except FetchDenied:
        outcome = "denied"
    except FetchBlockedIp:
        outcome = "blocked_ip"
    except FetchTooLarge:
        outcome = "too_large"
    except FetchFailed:
        outcome = "failed"
    want = str(case.get("expected", {}).get("outcome", "ok"))
    return _result(case, outcome == want, f"outcome={outcome} want={want}")


def _run_normalize_board(case: dict[str, Any]) -> dict[str, Any]:
    from jobguard.ingestion import normalize_item, normalize_url
    from jobguard.sources.base import SourceItem
    from jobguard.sources.boards import BoardFeedAdapter, BoardSpec

    raw = case.get("input", {})
    board = BoardSpec(
        provider=raw.get("provider", "greenhouse"),
        slug=raw.get("board", {}).get("slug", "x"),
        company=raw.get("board", {}).get("company", ""),
    )
    adapter = BoardFeedAdapter(boards=[board])
    job = raw.get("job", {})
    if board.provider == "greenhouse":
        item = adapter.map_greenhouse(board, job)
    else:
        item = adapter.map_lever(board, job)
    assert isinstance(item, SourceItem)
    post = normalize_item(item)
    want = case.get("expected", {})
    passed = (
        post.company == want.get("company")
        and post.role == want.get("role")
        and post.location == want.get("location")
        and normalize_url(post.link) == normalize_url(str(want.get("link", "")))
    )
    return _result(case, passed, f"got {post.company}/{post.role}/{post.location}/{post.link}")


def _run_sync_idempotent(case: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from jobguard.ingestion import Prefilter, sync_source
    from jobguard.sources.base import SourceItem
    from jobguard.store import Posting, init_db

    class DictAdapter:
        def __init__(self, items: list[SourceItem]) -> None:
            self._items = items

        @property
        def source_name(self) -> str:
            return "eval-src"

        def fetch(self, since: str) -> list[SourceItem]:
            return list(self._items)

        def cursor_after(self, items: list[SourceItem], since: str) -> str:
            return "cursor-eval"

    raw = case.get("input", {})
    items = []
    for entry in raw.get("items", []):
        payload = {
            "sender": entry.get("sender", ""),
            "parsed": entry.get("parsed"),
        }
        items.append(
            SourceItem(
                source=entry.get("source", "gmail"),
                external_id=entry.get("external_id", ""),
                payload=payload,
            )
        )
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        sync_source(DictAdapter(items), session, Prefilter())
        first_count = session.query(Posting).count()
        second = sync_source(DictAdapter(items), session, Prefilter())
        final_count = session.query(Posting).count()
    want = case.get("expected", {})
    passed = (
        first_count == int(want.get("postings", 0))
        and second.new_postings == int(want.get("second_new", 0))
        and second.skipped_seen == int(want.get("second_skipped", 0))
        and final_count == first_count
    )
    return _result(
        case,
        passed,
        f"postings={first_count} second_new={second.new_postings} "
        f"second_skipped={second.skipped_seen}",
    )


def _run_hidden_text(case: dict[str, Any]) -> dict[str, Any]:
    from jobguard.guardrails import strip_hidden_text

    raw = case.get("input", {})
    report = strip_hidden_text(str(raw.get("html", "")))
    want = case.get("expected", {})
    kept_ok = all(snippet in report.text for snippet in want.get("kept", []))
    dropped_ok = all(snippet not in report.text for snippet in want.get("dropped", []))
    removed_ok = bool(report.removed) == bool(want.get("removed_any", False))
    return _result(
        case,
        kept_ok and dropped_ok and removed_ok,
        f"kept={kept_ok} dropped={dropped_ok} removed_any={bool(report.removed)}",
    )


def _run_injection_scan(case: dict[str, Any]) -> dict[str, Any]:
    from jobguard.guardrails import scan_injection

    report = scan_injection(str(case.get("input", {}).get("text", "")))
    want = bool(case.get("expected", {}).get("flagged", False))
    rules = sorted(f.rule for f in report.findings)
    return _result(case, report.flagged == want, f"flagged={report.flagged} rules={rules}")


def _run_pii_redact(case: dict[str, Any]) -> dict[str, Any]:
    from jobguard.guardrails import redact_pii

    report = redact_pii(str(case.get("input", {}).get("text", "")))
    want = case.get("expected", {})
    leaked = [secret for secret in want.get("leaked", []) if secret in report.text]
    missing = [ph for ph in want.get("placeholders", []) if ph not in report.text]
    dropped = [kept for kept in want.get("kept", []) if kept not in report.text]
    passed = not leaked and not missing and not dropped
    return _result(case, passed, f"leaked={leaked} missing={missing} dropped={dropped}")


def _run_scam_check(case: dict[str, Any]) -> dict[str, Any]:
    from jobguard.guardrails import check_scam

    raw = case.get("input", {})
    report = check_scam(
        company=str(raw.get("company", "")),
        sender=str(raw.get("sender", "")),
        link=str(raw.get("link", "")),
        text=str(raw.get("text", "")),
    )
    want = case.get("expected", {})
    min_score = int(want.get("min_score", 0 if not want.get("flagged") else 2))
    passed = report.flagged == bool(want.get("flagged", False)) and (
        report.score >= min_score if report.flagged else report.score == min_score
    )
    signals = sorted(s.signal for s in report.signals)
    return _result(case, passed, f"flagged={report.flagged} score={report.score} signals={signals}")


def _run_link_check(case: dict[str, Any]) -> dict[str, Any]:
    from jobguard.guardrails import check_links

    report = check_links(str(case.get("input", {}).get("text", "")))
    want = case.get("expected", {})
    reasons = sorted(f.reason for f in report.flagged)
    passed = len(report.flagged) == int(want.get("flagged_count", 0))
    if "safe_count" in want:
        passed = passed and len(report.safe) == int(want["safe_count"])
    return _result(case, passed, f"flagged={reasons} safe={len(report.safe)}")


def _run_pipeline_smoke(case: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from jobguard.agent import Profile, run_pipeline
    from jobguard.llm.fake import FakeClient
    from jobguard.resume import Resume, ResumeItem
    from jobguard.store import Posting, init_db

    raw = case.get("input", {})
    rules = []
    for marker, key in [
        ("[classify]", "classify_json"),
        ("[extract]", "extract_json"),
        ("[match]", "match_json"),
        ("[suggest]", "suggest_json"),
    ]:
        if key in raw:
            rules.append((marker, str(raw[key])))
    client = FakeClient(rules=rules, default="NOT-JSON")
    profile_raw = raw.get("profile", {})
    profile = Profile(
        skills=list(profile_raw.get("skills", [])),
        role_types=list(profile_raw.get("role_types", [])),
        locations=list(profile_raw.get("locations", [])),
    )
    resume = Resume(
        owner="eval",
        items=[ResumeItem(id=str(rid)) for rid in raw.get("resume_ids", [])],
    )
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        from jobguard.ingestion import canonical_key as _key

        post = Posting(
            company="Acme",
            role="SDE Intern",
            canonical_key=_key("Acme", "SDE Intern", "", ""),
        )
        session.add(post)
        session.commit()
        result = run_pipeline(
            post,
            body=str(raw.get("body", "")),
            sender=str(raw.get("sender", "")),
            client=client,
            profile=profile,
            resume=resume,
            session=session,
        )
    want = case.get("expected", {})
    passed = result.status == want.get("status")
    if "score" in want:
        passed = passed and result.score == float(want["score"])
    return _result(case, passed, f"status={result.status} score={result.score}")


def _run_rank_order(case: dict[str, Any]) -> dict[str, Any]:
    from jobguard.agent import Profile, score_posting

    raw = case.get("input", {})
    profile = Profile(**raw.get("profile", {}))
    scores = [
        score_posting(
            profile,
            role=p.get("role", ""),
            location=p.get("location", ""),
            skills=list(p.get("skills", [])),
            text=p.get("text", ""),
        ).score
        for p in raw.get("postings", [])
    ]
    top = scores.index(max(scores)) if scores else -1
    want = int(case.get("expected", {}).get("top", 0))
    return _result(case, top == want, f"scores={scores} top={top}")


def _run_fabrication(case: dict[str, Any]) -> dict[str, Any]:
    from jobguard.llm.schemas import TailoringCard
    from jobguard.resume import Resume, ResumeItem, check_card

    raw = case.get("input", {})
    card = TailoringCard.model_validate(raw.get("card", {}))
    resume = Resume(
        owner="eval",
        items=[ResumeItem(id=str(rid)) for rid in raw.get("resume_ids", [])],
    )
    violations = check_card(card, resume)
    want = bool(case.get("expected", {}).get("violations", False))
    return _result(case, bool(violations) == want, f"violations={violations}")


def _run_digest_build(case: dict[str, Any]) -> dict[str, Any]:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from jobguard.digest import build_digest, record_sent
    from jobguard.ingestion import canonical_key as _key
    from jobguard.store import PendingAction, Posting, Suggestion, init_db

    raw = case.get("input", {})
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        for index, entry in enumerate(raw.get("postings", [])):
            deadline_h = entry.get("deadline_h")
            deadline = (
                datetime.now(UTC) + timedelta(hours=float(deadline_h))
                if deadline_h is not None
                else None
            )
            session.add(
                Posting(
                    company=str(entry.get("company", "")),
                    role=str(entry.get("role", "")),
                    location=str(entry.get("location", "")),
                    link=str(entry.get("link", "")),
                    score=float(entry.get("score", 0.0)),
                    scam_flag=bool(entry.get("scam", False)),
                    status=str(entry.get("status", "new")),
                    deadline=deadline,
                    canonical_key=_key(
                        str(entry.get("company", "")),
                        str(entry.get("role", "")),
                        str(entry.get("location", "")),
                        str(entry.get("link", "")),
                    )
                    + f"-{index}",
                )
            )
        session.commit()
        posts = session.query(Posting).order_by(Posting.id).all()
        for index, texts in raw.get("suggestions", {}).items():
            session.add(
                Suggestion(
                    posting_id=posts[int(index)].id,
                    card={"suggestions": [{"text": t} for t in texts]},
                    resume_item_ids=[],
                    gaps=[],
                )
            )
        for _ in range(int(raw.get("pending", 0))):
            session.add(
                PendingAction(
                    plan={},
                    risk="high",
                    status="pending",
                    token_hash=f"eval-{_}",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        session.commit()
        digest = build_digest(
            session,
            kind="morning",
            best_score=float(raw.get("best_score", 75.0)),
            urgent_hours=int(raw.get("urgent_hours", 48)),
        )
        if raw.get("record_between"):
            record_sent(session, "morning:eval", digest)
            digest = build_digest(
                session,
                kind="evening",
                best_score=float(raw.get("best_score", 75.0)),
                urgent_hours=int(raw.get("urgent_hours", 48)),
            )
        by_id = {p.id: i for i, p in enumerate(posts)}
        want = case.get("expected", {})
        got = {
            "closing": sorted(by_id[p.id] for p in digest.closing),
            "best": sorted(by_id[p.id] for p in digest.best),
            "flagged": sorted(by_id[p.id] for p in digest.flagged),
            "pending": digest.pending_approvals,
            "empty": digest.is_empty,
        }
        passed = all(got[k] == want.get(k) for k in got)
        return _result(case, passed, f"got={got} want={want}")


def _run_send_policy(case: dict[str, Any], owner_email: str) -> dict[str, Any]:
    from datetime import UTC, datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from jobguard.notify import check_sendable
    from jobguard.store import DigestSent, init_db

    raw = _substitute(dict(case.get("input", {})), owner_email)
    owner = str(raw.get("owner", owner_email))
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        for i in range(int(raw.get("sent_today", 0))):
            session.add(DigestSent(digest_id=f"eval-{i}", items=[], sent_at=datetime.now(UTC)))
        session.commit()
        allowed, reason = check_sendable(
            session,
            to=str(raw.get("to", "")),
            owner_email=owner,
            max_per_day=int(raw.get("cap", 20)),
        )
    want = bool(case.get("expected", {}).get("allowed", False))
    return _result(case, allowed == want, f"allowed={allowed} ({reason})")


def _run_outbound_scan(case: dict[str, Any]) -> dict[str, Any]:
    from jobguard.notify import scan_outbound

    raw = case.get("input", {})
    hits = scan_outbound(str(raw.get("subject", "")), str(raw.get("body", "")))
    want = bool(case.get("expected", {}).get("blocked", False))
    return _result(case, bool(hits) == want, f"hits={hits}")


def _run_approval_flow(case: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from jobguard.policy import ApprovalError, create_action, decide, verify_token
    from jobguard.store import init_db

    raw = case.get("input", {})
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        action, token = create_action(
            session,
            plan=dict(raw.get("plan", {})),
            risk=str(raw.get("risk", "high")),
            expires_hours=int(raw.get("expires_hours", 72)),
        )
        verified = verify_token(session, token) is not None
        try:
            decide(session, action, approved=True)
            first_ok = True
        except ApprovalError:
            first_ok = False
        try:
            decide(session, action, approved=True)
            double_raises = False
        except ApprovalError:
            double_raises = True
        second_none = verify_token(session, token) is None
    want = case.get("expected", {})
    passed = (
        verified == bool(want.get("verified", False))
        and first_ok == bool(want.get("first_ok", True))
        and double_raises == bool(want.get("double_decide_raises", True))
        and second_none == bool(want.get("second_verify_none", True))
    )
    detail = f"verified={verified} first_ok={first_ok} double_raises={double_raises}"
    return _result(case, passed, detail)


def _run_digest_split(case: dict[str, Any]) -> dict[str, Any]:
    from jobguard.sources import split_digest_jobs

    raw = case.get("input", {})
    jobs = split_digest_jobs(
        str(raw.get("sender", "")), str(raw.get("subject", "")), str(raw.get("body", ""))
    )
    want = case.get("expected", {})
    first = jobs[0] if jobs else None
    passed = len(jobs) == int(want.get("count", 0))
    if first is not None and "first_company" in want:
        passed = passed and first.company == want["first_company"]
    if first is not None and "first_role" in want:
        passed = passed and first.role == want["first_role"]
    first_desc = f"{first.company}/{first.role}" if first else "none"
    return _result(case, passed, f"count={len(jobs)} first={first_desc}")


def run_evals(
    *,
    cases_dir: str = "data/evals",
    baseline_path: str = "evals/baseline.json",
    out_path: str = "evals/metrics.json",
    owner_email: str = "owner@example.com",
) -> dict[str, Any]:
    """Run all cases, write metrics JSON, and return metrics + regressions."""
    cases = load_cases(cases_dir)
    results = [run_case(c, owner_email) for c in cases]
    metrics: dict[str, Any] = summarize(results)
    metrics["results"] = results
    regressions: list[str] = []
    baseline_file = Path(baseline_path)
    if baseline_file.exists():
        baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
        regressions = compare_with_baseline(metrics, baseline)
    metrics["regressions"] = regressions
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    serializable = {k: v for k, v in metrics.items() if k != "results"}
    Path(out_path).write_text(json.dumps(serializable, indent=2) + "\n", encoding="utf-8")
    return metrics
