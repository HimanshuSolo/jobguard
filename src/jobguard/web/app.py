"""Dashboard: FastAPI + Jinja, single-user login via Clerk (or password).

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
from urllib.parse import urlencode

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
from jobguard.web.clerk import ClerkAuth, ClerkConfig, ClerkResult

TEMPLATES_DIR = Path(__file__).parent / "templates"
LOGIN_RATE_LIMIT = 10
BOARD_PAGE_SIZE = 40
LOGIN_WINDOW_S = 300

_login_attempts: defaultdict[str, list[float]] = defaultdict(list)


def _safe_url(value: object) -> str:
    """Only http(s) links reach an href (alert emails are untrusted)."""
    text = str(value or "").strip()
    return text if text.lower().startswith(("https://", "http://")) else ""


def _asset_version() -> str:
    """Short hash of the static files, appended as ?v= to their URLs."""
    import hashlib

    digest = hashlib.sha256()
    for path in sorted((TEMPLATES_DIR.parent / "static").glob("*")):
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def _make_env() -> Environment:
    from jobguard.digest.builder import effective_score, posted_label

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
    env.filters["safe_url"] = _safe_url
    env.filters["posted"] = posted_label
    env.filters["score"] = lambda p: f"{effective_score(p):.0f}"
    env.filters["scoreval"] = lambda p: max(0, min(100, round(effective_score(p))))
    env.filters["initial"] = lambda name: (str(name or "?").strip()[:1] or "?").upper()
    env.filters["hue"] = lambda name: sum(ord(c) for c in str(name or "")) * 47 % 360
    return env


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the dashboard app (pass explicit settings in tests)."""
    settings = settings or get_settings()
    engine = get_engine(settings.database_url)
    init_db(engine)
    sessions = session_factory(engine)

    app = FastAPI(title="JobGuard", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(TEMPLATES_DIR.parent / "static")), name="static")

    clerk_config = ClerkConfig.from_settings(settings)
    clerk = ClerkAuth(clerk_config) if clerk_config else None
    env = _make_env()
    env.globals["clerk"] = {
        "enabled": clerk_config is not None,
        "publishable_key": clerk_config.publishable_key if clerk_config else "",
        "script_url": clerk_config.script_url if clerk_config else "",
        "ui_script_url": clerk_config.ui_script_url if clerk_config else "",
    }
    # Cache-busting: browsers kept serving the old stylesheet after redesigns.
    env.globals["asset_v"] = _asset_version()

    def _render(name: str, status_code: int = 200, **ctx: object) -> HTMLResponse:
        return HTMLResponse(env.get_template(name).render(**ctx), status_code=status_code)

    def clerk_state(request: Request) -> ClerkResult:
        cached = getattr(request.state, "clerk", None)
        if isinstance(cached, ClerkResult):
            return cached
        assert clerk is not None
        result = clerk.verify_cookies(dict(request.cookies))
        request.state.clerk = result
        return result

    def current_user(request: Request) -> str | None:
        if clerk is not None:
            result = clerk_state(request)
            return (result.email or result.user_id) if result.status == "ok" else None
        value = request.cookies.get(SESSION_COOKIE, "")
        if value and verify_session(
            value, settings.dashboard_username, settings.dashboard_secret_key
        ):
            return settings.dashboard_username
        return None

    def gate(request: Request, mode: str = "sign-in") -> HTMLResponse:
        """Clerk sign-in/up page; retries the original page once clerk-js has
        refreshed the (60 s) session cookie."""
        result = clerk_state(request)
        retry = request.url.path if request.method == "GET" else "/board"
        if request.method == "GET" and request.url.query:
            retry += "?" + request.url.query
        if retry.split("?")[0] in ("/login", "/sign-up", "/"):
            retry = "/board"
        status = 403 if result.status == "forbidden" else 401
        if request.url.path in ("/login", "/sign-up"):
            status = 403 if result.status == "forbidden" else 200
        return _render(
            "auth_gate.html",
            status_code=status,
            mode=mode,
            state=result.status,
            reason=result.reason,
            user_id=result.user_id,
            retry_url=retry,
        )

    def need_login(request: Request) -> HTMLResponse | RedirectResponse | None:
        if current_user(request) is not None:
            return None
        if clerk is not None:
            return gate(request)
        return RedirectResponse("/login", status_code=303)

    def csrf_for(request: Request) -> str:
        """The request's CSRF token, minting one (cookie set on the way out)."""
        token = request.cookies.get(CSRF_COOKIE, "") or getattr(request.state, "new_csrf", "")
        if not token:
            token = new_csrf_token()
            request.state.new_csrf = token
        return token

    def csrf_ok(request: Request, field: str) -> bool:
        return bool(field) and field == request.cookies.get(CSRF_COOKIE, "")

    csp = {
        "default-src": "'self'",
        "script-src": "'self'",
        "style-src": "'self' 'unsafe-inline'",
        "img-src": "'self' data:",
        "connect-src": "'self'",
        "frame-ancestors": "'none'",
        "base-uri": "'self'",
        "form-action": "'self'",
    }
    if clerk_config is not None:
        for directive, extra in clerk_config.csp_sources().items():
            csp[directive] = f"{csp.get(directive, '')} {extra}".strip()
        csp["worker-src"] = "'self' blob:"
    csp_header = "; ".join(f"{k} {v}" for k, v in csp.items())

    @app.middleware("http")
    async def security_headers(request: Request, call_next: object) -> object:
        response = await call_next(request)  # type: ignore[operator]
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = csp_header
        new_csrf = getattr(request.state, "new_csrf", "")
        if new_csrf:
            response.set_cookie(CSRF_COOKIE, new_csrf, samesite="lax", max_age=86400)
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Unauthenticated liveness probe (no data)."""
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> object:
        if clerk is not None:
            if current_user(request) is not None:
                return RedirectResponse("/board", status_code=303)
            return gate(request)
        return _render("login.html", error="")

    @app.get("/sign-up", response_class=HTMLResponse)
    def sign_up(request: Request) -> object:
        if clerk is None:
            return RedirectResponse("/login", status_code=303)
        if current_user(request) is not None:
            return RedirectResponse("/board", status_code=303)
        return gate(request, mode="sign-up")

    @app.post("/login")
    def login(request: Request, username: str = Form(""), password: str = Form("")) -> object:
        if clerk is not None:
            return RedirectResponse("/login", status_code=303)
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
        # Clerk sessions are ended client-side (Clerk.signOut); this clears ours.
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
        request: Request,
        view: str = "all",
        hide_scam: str = "",
        q: str = "",
        show_all: str = "",
        sort: str = "newest",
        page: int = 1,
    ) -> object:
        if (redirect := need_login(request)) is not None:
            return redirect
        from datetime import UTC, datetime

        from jobguard.digest.builder import HIDDEN_STATUSES, effective_score, posted_or_arrived

        def _on(value: str) -> bool:
            return value.strip().lower() not in ("", "0", "false", "no")

        with sessions() as session:
            everything = session.query(Posting).all()
        listed = (
            everything
            if _on(show_all)
            else [p for p in everything if p.pipe_status not in HIDDEN_STATUSES]
        )
        now = datetime.now(UTC).replace(tzinfo=None)

        def age_days(p: Posting) -> int:
            return max(0, (now - posted_or_arrived(p)).days)

        def is_remote(p: Posting) -> bool:
            return "remote" in (p.location or "").casefold()

        stats = {
            "open": sum(1 for p in listed if p.status != "ignored"),
            "week": sum(1 for p in listed if age_days(p) <= 7 and p.status != "ignored"),
            "applied": sum(1 for p in listed if p.status == "applied"),
            "analyzed": sum(1 for p in listed if p.pipe_status == "done"),
        }
        view = view if view in ("all", "week", "remote", "applied", "ignored") else "all"
        views = {
            "all": lambda p: p.status != "ignored",
            "week": lambda p: age_days(p) <= 7 and p.status != "ignored",
            "remote": lambda p: is_remote(p) and p.status != "ignored",
            "applied": lambda p: p.status == "applied",
            "ignored": lambda p: p.status == "ignored",
        }
        postings = [p for p in listed if views[view](p)]
        if _on(hide_scam):
            postings = [p for p in postings if not p.scam_flag]
        if q.strip():
            needle = q.strip().casefold()
            postings = [
                p
                for p in postings
                if needle in (p.role or "").casefold()
                or needle in (p.company or "").casefold()
                or needle in (p.location or "").casefold()
            ]
        sort = sort if sort in ("newest", "match") else "newest"
        if sort == "match":
            postings.sort(key=lambda p: (effective_score(p), posted_or_arrived(p)), reverse=True)
        else:
            postings.sort(key=lambda p: (posted_or_arrived(p), effective_score(p)), reverse=True)
        total = len(postings)
        pages = max(1, -(-total // BOARD_PAGE_SIZE))
        page = min(max(1, page), pages)
        shown = postings[(page - 1) * BOARD_PAGE_SIZE : page * BOARD_PAGE_SIZE]

        # Newest-first lists read best grouped by posting age.
        groups: list[tuple[str, list[Posting]]] = []
        for post in shown:
            if sort == "match":
                label = "Best match first"
            else:
                days = age_days(post)
                label = (
                    "Today"
                    if days == 0
                    else "Yesterday"
                    if days == 1
                    else "This week"
                    if days <= 7
                    else "This month"
                    if days <= 31
                    else "Older"
                )
            if not groups or groups[-1][0] != label:
                groups.append((label, []))
            groups[-1][1].append(post)

        base = {"q": q, "sort": sort, "hide_scam": hide_scam, "show_all": show_all}
        base = {k: v for k, v in base.items() if v and not (k == "sort" and v == "newest")}

        def link(**changes: object) -> str:
            params = {**base, "view": view, **changes}
            params = {k: v for k, v in params.items() if v and not (k == "view" and v == "all")}
            return "/board" + (f"?{urlencode(params)}" if params else "")

        return _render(
            "board.html",
            groups=groups,
            total=total,
            stats=stats,
            page=page,
            pages=pages,
            prev_url=link(page=page - 1) if page > 1 else "",
            next_url=link(page=page + 1) if page < pages else "",
            view_urls={name: link(view=name, page="") for name in views},
            view=view,
            hide_scam=hide_scam,
            q=q,
            show_all=show_all,
            sort=sort,
            csrf=csrf_for(request),
        )

    @app.post("/postings/{posting_id}/status")
    def set_status(
        request: Request, posting_id: int, value: str = Form(""), csrf: str = Form("")
    ) -> object:
        if (redirect := need_login(request)) is not None:
            return redirect
        if not csrf_ok(request, csrf):
            return _render("result.html", title="Rejected", message="CSRF check failed.")
        if value not in ("new", "applied", "ignored"):
            return _render("result.html", title="Rejected", message="Unknown status.")
        with sessions() as session:
            post = session.get(Posting, posting_id)
            if post is None:
                return _render("result.html", title="Not found", message="No such posting.")
            post.status = value
            session.commit()
        back = request.headers.get("referer", "")
        target = f"/postings/{posting_id}"
        if back:
            from urllib.parse import urlsplit

            parts = urlsplit(back)
            if parts.netloc == request.url.netloc and parts.path == "/board":
                target = parts.path + (f"?{parts.query}" if parts.query else "")
        return RedirectResponse(target, status_code=303)

    def render_detail(posting_id: int, notice: str, csrf_token: str) -> HTMLResponse:
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
            "detail.html",
            posting=post,
            suggestion=suggestion,
            matched=matched,
            missing=missing,
            notice=notice,
            csrf=csrf_token,
        )

    @app.get("/postings/{posting_id}", response_class=HTMLResponse)
    def posting_detail(request: Request, posting_id: int) -> object:
        if (redirect := need_login(request)) is not None:
            return redirect
        return render_detail(posting_id, "", csrf_for(request))

    @app.post("/postings/{posting_id}/analyze", response_class=HTMLResponse)
    def analyze_now(request: Request, posting_id: int, csrf: str = Form("")) -> object:
        if (redirect := need_login(request)) is not None:
            return redirect
        if not csrf_ok(request, csrf):
            return _render("result.html", title="Rejected", message="CSRF check failed.")
        from jobguard.agent.analyze import analyze_posting

        result = analyze_posting(posting_id, sessions=sessions, settings=settings)
        notice = (
            f"Analyze: {result.outcome} (status={result.status or '-'}; "
            f"{result.detail}; llm_calls={result.llm_calls})."
        )
        return render_detail(posting_id, notice, csrf_for(request))

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
        return confirm_page(token, action, csrf_for(request))

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
            csrf=csrf_for(request),
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
            csrf=csrf_for(request),
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
        from jobguard.worker import triage_stage

        triage_stage(sessions, settings)
        with sessions() as session:
            recent = session.query(AuditLog).order_by(AuditLog.id.desc()).limit(15).all()
        from jobguard.sources.configure import adapter_status

        return _render(
            "sources.html",
            adapters=adapter_status(settings),
            recent=recent,
            result="Sync done — " + "; ".join(lines),
            csrf=csrf_for(request),
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
            csrf=csrf_for(request),
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
