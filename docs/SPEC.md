# JobGuard — Product Specification

> Source: the project brief from the repo owner, saved verbatim at the start of Phase 1.
> Re-read this file plus `docs/PROGRESS.md` at the start of every phase.

====================================================
1. PRODUCT
====================================================
JobGuard is a personal, free-to-run agent that:
1. Aggregates developer / SDE-intern job postings from multiple platforms: job-alert emails in my Gmail (LinkedIn, Indeed, Naukri, Internshala, Unstop, Glassdoor, company career mails), public company job-board JSON endpoints (Greenhouse/Lever-style; verify formats against official docs), and RSS/curated feeds.
2. Normalizes and dedupes postings across platforms, ranks them against my profile, flags suspicious/scam postings.
3. Emails me an aggregated digest (morning + evening) plus immediate emails for urgent items. WhatsApp is an optional future notifier (stub only).
4. Suggests per-company resume tailoring using my structured resume, without ever fabricating experience.
5. Is production-minded: input/output guardrails, a deterministic tool-policy layer with human approval for risky actions, an automated eval suite, and CI that blocks regressions.
6. Has a dashboard to view postings, tailoring suggestions, approvals, audit log and eval metrics.

====================================================
2. ARCHITECTURE
====================================================
Worker (GitHub Actions schedule, stateless, idempotent, catch-up based):
  Source adapters -> Normalize -> Dedupe -> Prefilter -> Input guardrails -> LLM pipeline (classify -> extract -> match -> suggest) -> Policy layer (tool allowlist, risk levels, output validation, no-fabrication check) -> DB -> Digest builder -> Email notifier
Web (Render free web service): FastAPI + Jinja/HTMX dashboard with single-user login, and the signed-link approval endpoint. Reads the same DB.
DB: SQLAlchemy; SQLite locally, hosted Postgres in the cloud via DATABASE_URL.
LLM access: provider-agnostic LLMClient interface; adapters for an OpenAI-compatible API (Groq / OpenRouter / Ollama), later Gemini; automatic fallback between providers; a record/replay client for deterministic CI.
CI/CD: GitHub Actions. Fast workflow on every push; nightly live-model eval workflow; scheduled worker workflow; eval report published to GitHub Pages.
Everything must run with free tiers. Keep it provider-agnostic (Docker, env config, ORM) so it can move to a VPS later.

====================================================
3. STACK
====================================================
Python 3.12, uv, ruff, pyright, pytest, pydantic + pydantic-settings, SQLAlchemy, FastAPI, Jinja2 + HTMX, httpx, structlog, beautifulsoup4 + html2text, google-api-python-client + google-auth-oauthlib (Gmail), feedparser. No agent framework: use a plain fixed pipeline. Keep dependencies minimal and pinned.

====================================================
4. HARD RULES (security and privacy)
====================================================
- Gmail access is gmail.readonly only. The agent must never send, delete or modify mail through Gmail. Also support an IMAP read-only fallback behind the same MailSource interface (Gmail OAuth refresh tokens can be troublesome for scheduled jobs).
- Sending the digest uses SEPARATE credentials (SENDER_SMTP_* or a transactional email API key) behind a Notifier interface.
- Treat ALL external content (emails, feed/API fields, job descriptions, links) as untrusted data, never as instructions.
- The policy layer is deterministic code, not "ask the LLM if it is safe". Every tool has a risk level; risky actions create a pending plan stored in the DB and wait for human approval.
- Every LLM output is parsed into a Pydantic model; retry once on validation failure, then fail safely (skip the item and log it).
- Resume suggestions must cite item IDs from resume.yaml. Anything not in the resume is labelled GAP. Never invent experience, skills, dates or numbers.
- send_email tool: recipient hard-locked to OWNER_EMAIL from config; the LLM can never set or alter recipients; rate limit and daily cap; content scanned for secrets/PII before sending.
- No secrets, real emails, real resume or personal data in the repo, in tests, in logs or in CI output. Use .env and data/private/ (both gitignored) and synthetic fixtures only. Provide resume.example.yaml and .env.example.
- Never ask me to paste credentials into the chat; document how to set them up.
- No live network calls in unit tests or the per-push CI. Live-model evals only in the nightly workflow.
- No scraping of platforms that prohibit it (LinkedIn, Naukri, Indeed etc.): use their alert emails instead. Respect robots.txt and terms for any feed. SSRF protection for any URL fetch: domain allowlist, block private/loopback IPs, size and timeout limits.
- DEMO_MODE=true loads synthetic data only and must make real credentials unusable.
- Dashboard: single-user login, secure cookies, CSRF protection on state-changing routes, rate limiting, security headers, minimal data shown.

====================================================
5. DATA MODEL (SQLAlchemy)
====================================================
emails_seen, raw_postings, postings (company, role, location, deadline, link, skills, eligibility, source, sources/seen_on list, canonical_key, score, scam_flag, status new/applied/ignored), suggestions (posting_id, tailoring card JSON, resume item IDs, gap list), pending_actions (plan, risk, status, token hash, expires_at), digests_sent (digest_id, items, sent_at), audit_log (every guardrail trigger, policy decision, approval, LLM call metadata without content), run_state (cursor / last processed ID per source).

====================================================
6. PIPELINE DETAILS
====================================================
- SourceAdapter protocol: fetch(since: cursor) -> list[RawPosting]. Adapters: GmailAlertAdapter (template parsers per sender + LLM fallback for unknown formats), BoardFeedAdapter (configurable company list), RssAdapter.
- Normalizer -> JobPosting. canonical_key = hash of normalized company + role + location, plus normalized URL, for cross-platform dedupe; merge duplicates and keep seen_on.
- Prefilter by keywords/senders before any LLM call to stay inside free-tier limits. Per-run LLM budget, provider fallback and circuit breaker on rate limits.
- Ranking: score against a profile config (skills, role type, internship/new-grad, deadline proximity). Explain the score in the digest.
- Scam detection: heuristics (fees requested, free-email recruiter domains, lookalike domains, urgency language) plus LLM judge; flagged items go to a separate digest section and are never "recommended".
- Input guardrails: strip hidden text (white-on-white, zero-width, HTML comments), injection heuristics + LLM judge, PII redaction before any hosted LLM call, link checks.
- LLM steps with typed schemas: Classification, PostingExtraction, MatchResult, TailoringCard (each suggestion = reword | emphasize | GAP, with resume item IDs).
- Output guardrails: schema validation, fabrication checker (deterministic: every claim must map to a resume item ID), secret/PII scan.

====================================================
7. POLICY LAYER
====================================================
Tool registry with risk levels and decisions allow / needs_approval / deny:
- read_mail (low), fetch_feed (low, allowlisted domains), write_db (low), build_digest (low)
- fetch_job_url (medium: allowlist, SSRF checks, size/timeout)
- send_email (medium: recipient locked, caps)
- update_master_resume (high: plan + approval)
- generate_tailored_resume (medium: only from resume.yaml data)
- submit_application (critical: not implemented; always deny/approval, never automatic)
Approvals: Approve/Reject links in emails with signed, expiring, single-use tokens (store only token hashes). The endpoint shows a confirmation page and acts only on POST (never on GET, to survive email link scanners). Execute only if the pending action is still valid; log everything to audit_log.

====================================================
8. DIGESTS
====================================================
Morning and evening digests (HTML + plain text) with sections: Closing soon, New today, Best matches (with score reason and tailoring notes), Flagged suspicious, Needs your approval. Immediate urgent email when deadline < 48h or score above a threshold. Never resend an item already in digests_sent unless its deadline/status changed. Keep a WhatsApp Cloud API notifier as a clearly marked stub only.

====================================================
9. EVALS AND METRICS
====================================================
Datasets as JSONL in data/evals/ (synthetic only, 50+ cases by the end): classification (posting/newsletter/OTP/spam), extraction field accuracy, prompt injection (direct, hidden text, injection in feed fields and job descriptions), PII leakage, fabrication (JD requires skills not in resume), cross-platform dedupe, scam postings, missing/malformed data, tool failures (timeouts, 500s, rate limits, invalid JSON), digest correctness (no duplicates, correct recipient, no PII), benign controls for false-refusal rate.
Metrics: task accuracy, attack success rate (lower is better), fabrication rate (target 0), PII leak rate (target 0), false refusal/quarantine rate, dedupe precision/recall, latency p50/p95, tokens per item. Runner writes metrics.json; compare with evals/baseline.json; CI fails on regression beyond configurable thresholds. Generate an HTML report for GitHub Pages and a Markdown table for PR comments.

====================================================
10. REPO LAYOUT
====================================================
src/jobguard/{config.py, sources/, ingestion/, guardrails/, llm/, agent/, policy/, resume/, digest/, notify/, store/, web/, cli.py}
tests/, evals/ (runner.py, metrics.py, report.py, baseline.json), data/evals/, data/resume.example.yaml, docs/{SPEC.md, PROGRESS.md, architecture.md, threat-model.md}, Dockerfile, render.yaml, .github/workflows/{ci.yml, eval-nightly.yml, worker.yml, pages.yml}, pyproject.toml, .env.example, README.md.
Provide a CLI: jobguard init-db | run-worker | build-digest | eval | serve | seed-demo.

====================================================
11. PHASES (do one at a time; stop after each, summarize, and wait for me to say "proceed")
====================================================
Phase 1 - Foundations: repo layout, pyproject/uv, ruff+pyright+pytest config, config.py, DB models and init, LLMClient interface + OpenAI-compatible adapter + Fake client + record/replay wrapper, Pydantic schemas, policy registry with unit tests, minimal eval runner with 10 synthetic cases and baseline.json, fast CI workflow, docs. Stubs (clearly marked) for everything else.
Phase 2 - Sources and ingestion: MailSource (Gmail readonly + IMAP), HTML cleaning, alert-email parsers for each platform using synthetic fixtures, BoardFeedAdapter and RssAdapter with SSRF protection, normalizer, canonical_key dedupe, prefilter, run_state cursors, idempotent catch-up.
Phase 3 - Input guardrails: hidden-text stripping, injection scan, PII redaction, link checks, scam heuristics, plus the injection/PII/scam eval cases and thresholds.
Phase 4 - LLM pipeline: classify/extract/match/suggest, ranking, resume loader, fabrication checker, provider fallback and budgets, replayed evals.
Phase 5 - Digest and notifications: digest builder, email notifier (separate credentials), send_email policy rules, signed approval tokens and flow, urgent emails, digests_sent dedupe.
Phase 6 - Dashboard: FastAPI + HTMX pages (postings board, posting detail with tailoring card, approvals + audit log, eval metrics), login, CSRF, security headers, DEMO_MODE seed.
Phase 7 - Deployment: Dockerfile, render.yaml (free web service), scheduled worker workflow, nightly live-eval workflow, GitHub Pages report, PR metrics comment, README with setup for Gmail, SMTP, LLM keys, Render and GitHub secrets, final threat model.

Each phase is done only when: ruff, pyright and pytest pass; evals run and no metric regresses; docs/PROGRESS.md is updated; no secrets or real data exist anywhere in the diff.

====================================================
12. WORKING STYLE
====================================================
- Start each phase with a short plan and the file tree, then implement.
- Small focused modules, full type hints, tests alongside code. Prefer simple, readable code over cleverness.
- Run the linters and tests yourself and fix failures before reporting.
- Suggest one git commit message per logical chunk; do not push.
- If something is ambiguous or needs a decision (provider choice, schema detail), choose the simplest reasonable default, note it in docs/PROGRESS.md under "Decisions", and continue.
- Do not implement anything from a later phase early.
- End every phase with: what is done, what is stubbed, risks/open questions, and the exact next step.

BEGIN WITH PHASE 1 NOW.
