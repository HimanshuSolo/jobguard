"""Dashboard: FastAPI + Jinja/HTMX, single-user login (Phase 6).

Pages: login, postings board (filterable), posting detail with tailoring
card, approvals (list + confirm + POST-only decide), audit log, eval
metrics. Token links (`/approvals/{token}`) serve email recipients without a
login; the dashboard list uses `/approvals/by-id/{id}` behind the session.
GET never mutates — every state change is a POST with CSRF validation.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from jobguard.config import Settings, get_settings
from jobguard.policy import ApprovalError, decide, verify_token
from jobguard.store import (
    AuditLog,
    PendingAction,
    Posting,
    Suggestion,
    get_engine,
    init_db,
    session_factory,
)
from jobguard.web.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    create_session,
    new_csrf_token,
    verify_password,
    verify_session,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
LOGIN_RATE_LIMIT = 10
LOGIN_WINDOW_S = 300

_login_attempts: defaultdict[str, list[float]] = defaultdict(list)


def _templates() -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)


def _render(name: str, **ctx: object) -> HTMLResponse:
    return HTMLResponse(_templates().get_template(name).render(**ctx))


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the dashboard app (pass explicit settings in tests)."""
    settings = settings or get_settings()
    engine = get_engine(settings.database_url)
    init_db(engine)
    sessions = session_factory(engine)

    app = FastAPI(title="JobGuard", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(TEMPLATES_DIR.parent / "static")), name="static")

    def current_user(request: Request) -> str | None:
        value = request.cookies.get(SESSION_COOKIE, "")
        if value and verify_session(
            value, settings.dashboard_username, settings.dashboard_secret_key
        ):
            return settings.dashboard_username
        return None

    def need_login(request: Request) -> RedirectResponse | None:
        if current_user(request) is None:
            return RedirectResponse("/login", status_code=303)
        return None

    def csrf_ok(request: Request, field: str) -> bool:
        return bool(field) and field == request.cookies.get(CSRF_COOKIE, "")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: object) -> object:
        response = await call_next(request)  # type: ignore[operator]
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://unpkg.com; "
            "style-src 'self' 'unsafe-inline'"
        )
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login_form() -> HTMLResponse:
        return _render("login.html", error="")

    @app.post("/login")
    def login(request: Request, username: str = Form(""), password: str = Form("")) -> object:
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        attempts = [t for t in _login_attempts[ip] if now - t < LOGIN_WINDOW_S]
        _login_attempts[ip] = attempts
        if len(attempts) >= LOGIN_RATE_LIMIT:
            return _render("login.html", error="Too many attempts; try again later.")
        if username != settings.dashboard_username or not verify_password(
            password, settings.dashboard_password_hash
        ):
            attempts.append(now)
            _login_attempts[ip] = attempts
            return _render("login.html", error="Invalid credentials.")
        _login_attempts.pop(ip, None)
        response = RedirectResponse("/board", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            create_session(username, settings.dashboard_secret_key),
            httponly=True,
            samesite="lax",
            max_age=86400,
        )
        response.set_cookie(CSRF_COOKIE, new_csrf_token(), samesite="lax", max_age=86400)
        return response

    @app.post("/logout")
    def logout() -> RedirectResponse:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie(CSRF_COOKIE)
        return response

    @app.get("/")
    def index(request: Request) -> object:
        if current_user(request) is None:
            return RedirectResponse("/login", status_code=303)
        return RedirectResponse("/board", status_code=303)

    @app.get("/board", response_class=HTMLResponse)
    def board(
        request: Request, status: str = "", hide_scam: str = "", intern: str = "1", q: str = ""
    ) -> object:
        if (redirect := need_login(request)) is not None:
            return redirect
        with sessions() as session:
            query = session.query(Posting).order_by(Posting.score.desc())
            if status in ("new", "applied", "ignored"):
                query = query.filter_by(status=status)

            def _on(value: str) -> bool:
                return value.strip().lower() not in ("", "0", "false", "no")

            postings = query.all()
            if _on(hide_scam):
                postings = [p for p in postings if not p.scam_flag]
            if _on(intern):
                postings = [p for p in postings if "intern" in (p.role or "").casefold()]
            if q.strip():
                needle = q.strip().casefold()
                postings = [
                    p
                    for p in postings
                    if needle in (p.role or "").casefold() or needle in (p.company or "").casefold()
                ]
        return _render(
            "board.html", postings=postings, status=status, hide_scam=hide_scam, intern=intern, q=q
        )

    @app.get("/postings/{posting_id}", response_class=HTMLResponse)
    def posting_detail(request: Request, posting_id: int) -> object:
        if (redirect := need_login(request)) is not None:
            return redirect
        from jobguard.resume import keyword_gap, load_resume

        with sessions() as session:
            post = session.get(Posting, posting_id)
            if post is None:
                return _render("result.html", title="Not found", message="No such posting.")
            suggestion = (
                session.query(Suggestion)
                .filter_by(posting_id=post.id)
                .order_by(Suggestion.id.desc())
                .first()
            )
            body, role, company = post.body or "", post.role or "", post.company or ""
        try:
            resume = load_resume(settings.resume_path)
        except (FileNotFoundError, ValueError):
            from jobguard.resume import Resume

            resume = Resume(owner="", headline="")
        matched, missing = keyword_gap(f"{role}\n{company}\n{body}", resume)
        return _render(
            "detail.html", posting=post, suggestion=suggestion, matched=matched, missing=missing
        )

    @app.get("/approvals", response_class=HTMLResponse)
    def approvals(request: Request) -> object:
        if (redirect := need_login(request)) is not None:
            return redirect
        with sessions() as session:
            pending = (
                session.query(PendingAction)
                .filter_by(status="pending")
                .order_by(PendingAction.id)
                .all()
            )
            decided = (
                session.query(PendingAction)
                .filter(PendingAction.status != "pending")
                .order_by(PendingAction.id.desc())
                .limit(20)
                .all()
            )
        return _render("approvals.html", actions=pending, decided=decided)

    def confirm_page(token: str, action: PendingAction, csrf: str) -> HTMLResponse:
        return _render("approval_confirm.html", action=action, token=token, csrf=csrf)

    @app.get("/approvals/{token}", response_class=HTMLResponse)
    def approval_by_token(token: str, request: Request) -> object:
        with sessions() as session:
            action = verify_token(session, token)
            if action is None:
                return _render(
                    "result.html", title="Invalid link", message="Expired, used, or unknown token."
                )
        # Read-only GET: hand the bearer a CSRF cookie so their later POST
        # validates. Scanners only GET, so they can never trigger execution.
        csrf = request.cookies.get(CSRF_COOKIE, "") or new_csrf_token()
        response = confirm_page(token, action, csrf)
        if not request.cookies.get(CSRF_COOKIE):
            response.set_cookie(CSRF_COOKIE, csrf, samesite="lax", max_age=86400)
        return response

    @app.get("/approvals/by-id/{action_id}", response_class=HTMLResponse)
    def approval_by_id(request: Request, action_id: int) -> object:
        if (redirect := need_login(request)) is not None:
            return redirect
        with sessions() as session:
            action = session.get(PendingAction, action_id)
            if action is None or action.status != "pending":
                return _render("result.html", title="Not pending", message="Nothing to review.")
            action_id_value = action.id
        return _render(
            "approval_confirm.html",
            action=action,
            token=f"id:{action_id_value}",
            csrf=request.cookies.get(CSRF_COOKIE, ""),
        )

    def execute(token: str, decision: str, csrf_field: str, request: Request) -> object:
        if not csrf_ok(request, csrf_field):
            return _render("result.html", title="Rejected", message="CSRF check failed.")
        with sessions() as session:
            if token.startswith("id:"):
                action = session.get(PendingAction, int(token[3:]))
                if action is None or action.status != "pending":
                    return _render("result.html", title="Not pending", message="Nothing to review.")
                stored = action
            else:
                stored = verify_token(session, token)
                if stored is None:
                    return _render(
                        "result.html", title="Invalid link", message="Expired or already used."
                    )
            try:
                outcome = decide(
                    session, stored, approved=(decision == "approve"), executor=_execute_plan
                )
            except ApprovalError as exc:
                return _render("result.html", title="Not applied", message=str(exc))
        message = (
            f"Rejected action #{stored.id}."
            if decision != "approve"
            else (f"Approved action #{stored.id}." + (f" {outcome}" if outcome else ""))
        )
        return _render("result.html", title="Done", message=message)

    @app.post("/approvals/{token}")
    def approval_decide(
        token: str, request: Request, decision: str = Form(""), csrf: str = Form("")
    ) -> object:
        # Token links are bearer: no login needed (same as the email flow).
        return execute(token, decision, csrf, request)

    @app.get("/sources", response_class=HTMLResponse)
    def sources(request: Request) -> object:
        if (redirect := need_login(request)) is not None:
            return redirect
        from jobguard.sources.configure import adapter_status

        with sessions() as session:
            recent = session.query(AuditLog).order_by(AuditLog.id.desc()).limit(15).all()
        return _render(
            "sources.html",
            adapters=adapter_status(settings),
            recent=recent,
            result="",
            csrf=request.cookies.get(CSRF_COOKIE, ""),
        )

    @app.post("/sync")
    def sync_now(request: Request, csrf: str = Form("")) -> object:
        if (redirect := need_login(request)) is not None:
            return redirect
        if not csrf_ok(request, csrf):
            return _render("result.html", title="Rejected", message="CSRF check failed.")
        from jobguard.ingestion import sync_source
        from jobguard.sources.configure import build_adapters, build_prefilter

        with sessions() as session:
            adapters, skipped = build_adapters(settings)
            prefilter = build_prefilter(settings)
            lines: list[str] = []
            for adapter in adapters:
                result = sync_source(adapter, session, prefilter)
                lines.append(
                    f"{result.source}: +{result.new_postings} new, "
                    f"{result.merged_postings} merged, {result.filtered_out} filtered"
                )
            lines.extend(skipped)
            session.commit()
            recent = session.query(AuditLog).order_by(AuditLog.id.desc()).limit(15).all()
        from jobguard.sources.configure import adapter_status

        return _render(
            "sources.html",
            adapters=adapter_status(settings),
            recent=recent,
            result="Sync done — " + "; ".join(lines),
            csrf=request.cookies.get(CSRF_COOKIE, ""),
        )

    @app.get("/resume", response_class=HTMLResponse)
    def resume_page(request: Request, message: str = "", error: str = "") -> object:
        if (redirect := need_login(request)) is not None:
            return redirect
        from jobguard.resume import Resume, load_resume

        try:
            resume = load_resume(settings.resume_path)
        except (FileNotFoundError, ValueError):
            resume = Resume(owner="", headline="")
        return _render(
            "resume.html",
            resume=resume,
            message=message,
            error=error,
            csrf=request.cookies.get(CSRF_COOKIE, ""),
        )

    @app.post("/resume")
    def resume_save(request: Request, content: str = Form(""), csrf: str = Form("")) -> object:
        if (redirect := need_login(request)) is not None:
            return redirect
        if not csrf_ok(request, csrf):
            return _render("result.html", title="Rejected", message="CSRF check failed.")
        from jobguard.resume import resume_dict_from_text, write_resume_yaml

        try:
            if not content.strip():
                raise ValueError("empty resume — nothing saved")
            data = resume_dict_from_text(content)
            write_resume_yaml(data, settings.resume_path)
        except ValueError as exc:
            return resume_page(request, error=f"Could not parse resume: {exc}")
        return resume_page(request, message="Resume saved. New suggestions will use it.")

    @app.get("/audit", response_class=HTMLResponse)
    def audit(request: Request) -> object:
        if (redirect := need_login(request)) is not None:
            return redirect
        with sessions() as session:
            entries = session.query(AuditLog).order_by(AuditLog.id.desc()).limit(200).all()
        return _render("audit.html", entries=entries)

    @app.get("/metrics", response_class=HTMLResponse)
    def metrics(request: Request) -> object:
        if (redirect := need_login(request)) is not None:
            return redirect
        data = _load_metrics()
        return _render("metrics.html", metrics=data)

    return app


def _execute_plan(plan: dict[str, object]) -> str:
    """Run an approved low/medium-risk plan (only known plan kinds)."""
    from jobguard.policy import evaluate
    from jobguard.policy.registry import PolicyContext

    kind = str(plan.get("kind", ""))
    if kind == "generate_tailored_resume":
        out = Path("data/private") / f"tailored-{plan.get('posting_key', 'unknown')}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            f"# Tailored resume draft\n\nPosting: {plan.get('posting_key', '')}\n\n"
            f"Notes: {plan.get('notes', '')}\n",
            encoding="utf-8",
        )
        verdict = evaluate(PolicyContext(tool="generate_tailored_resume"))
        return f"Wrote {out} (policy: {verdict.decision})."
    return f"No executor registered for plan kind {kind!r}; approval recorded without effect."


def _load_metrics() -> dict[str, object] | None:
    path = Path("evals/metrics.json")
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
        except ValueError:
            return None
    return None
