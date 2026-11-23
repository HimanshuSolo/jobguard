# JobGuard — Progress Tracker

> Updated at the end of every phase. Phase is done only when:
> `ruff`, `pyright`, `pytest` pass; evals run with no metric regression;
> this file is updated; no secrets or real data exist anywhere in the diff.

## Phase checklist

- [x] Phase 1 — Foundations (done)
- [x] Phase 2 — Sources and ingestion (done)
- [x] Phase 3 — Input guardrails (done)
- [x] Phase 4 — LLM pipeline (done)
- [x] Phase 5 — Digest and notifications (done)
- [x] Phase 6 — Dashboard (done)
- [x] Phase 7 — Deployment (done)
- [ ] Phase 8 — Minimalist frontend revamp + real-data hardening (in progress)

## Phase 1 — Foundations

Scope: repo layout, `pyproject`/uv, ruff+pyright+pytest config, `config.py`,
DB models + init, `LLMClient` interface + OpenAI-compatible adapter + Fake
client + record/replay wrapper, Pydantic schemas, policy registry with unit
tests, minimal eval runner with 10 synthetic cases + `baseline.json`, fast CI
workflow, docs. Stubs (clearly marked) for everything else.

Status: in progress.

### Decisions

- Python: brief asks 3.12, but this machine has 3.14 only, so
  `requires-python = ">=3.12"` and the local venv runs 3.14. CI pins 3.12.
  Code stays 3.12-compatible (no 3.13+/3.14-only syntax).
- Sync `LLMClient.complete()` (no async): the worker is a batch pipeline and
  sync keeps the interface, Fake client, and record/replay trivially
  deterministic. Async can be added later without changing schemas.
- Config: single `Settings` (pydantic-settings, `.env` file) with an explicit
  `require_live_credentials()` guard that raises when `DEMO_MODE=true`.
- Policy `evaluate()` takes an explicit `PolicyContext` value object so unit
  tests and evals exercise it with zero I/O.
- Phase-1 eval cases cover only what Phase 1 implements (policy decisions,
  schema validation, replay determinism, demo-mode guard). Later phases append
  their own case files; the runner aggregates all `data/evals/*.jsonl`.
- Package layout: `src/` layout with `jobguard` package; entry script
  `jobguard = jobguard.cli:main`.
- `evals/` (runner code) is top-level per the brief's repo layout; generated
  `evals/metrics.json` is gitignored, `evals/baseline.json` is committed.

### Done criteria checks (2026-11-23, all observed in-session)

- [x] `ruff check` passes (`All checks passed!`)
- [x] `ruff format --check` passes (`38 files already formatted`)
- [x] `pyright` passes (`0 errors, 0 warnings`)
- [x] `pytest` passes (`36 passed`, deprecation warnings promoted to errors)
- [x] `jobguard eval` passes with no regression vs `evals/baseline.json`
  (`accuracy=1.000 passed=10/10`, exit 0)
- [x] No secrets / real data in the diff (scanned; no `.env` file exists)

## Phase 2 — Sources and ingestion

Scope: `MailSource` (Gmail readonly via injected service + IMAP read-only
fallback), HTML cleaning, per-sender alert parsers with synthetic fixtures,
`BoardFeedAdapter` (Greenhouse/Lever formats verified against current public
docs), `RssAdapter` with SSRF-safe fetch (allowlist, public-IP-only DNS,
size/timeout caps, redirect confinement), normalizer + `canonical_key` dedupe,
keyword/sender prefilter, `run_state` cursors, idempotent `sync_source`, and a
working `run-worker` (boards/RSS/Gmail/IMAP; skips whatever is unconfigured).

Status: done. Gates 2026-11-23: ruff clean, pyright 0 errors, pytest 88
passed, eval 24/24 vs baseline v2 (10 phase-1 + 14 phase-2 cases).

### Decisions (Phase 2)

- Greenhouse list endpoint queried WITHOUT `?content=true` by default
  (10 MB+ boards would blow the worker); per-board `include_content` opt-in.
- `canonical_key` keeps the spec'd URL component, but `merge_duplicates`
  runs a second pass merging identical (company, role, location) triples with
  different links (LinkedIn redirect vs company apply URL). Fully empty
  postings never merge.
- Unknown mail senders pass the sender prefilter gate (company career mail);
  known platforms must be listed. Keyword gate still applies to all mail.
- IMAP `SEARCH` uses explicit `CHARSET UTF-8` (typed cleanly vs `None`).
- RSS dates normalized to ISO via feedparser's parsed structs (RFC822 strings
  do not sort lexicographically).
- Board formats verified via current public docs/search (Greenhouse Job Board
  API, Lever postings API shapes); recorded in `docs/architecture.md` below.

## Phase 3 — Input guardrails

Scope: `strip_hidden_text` (comments, zero-width, invisible styles),
`scan_injection` (6 rules + benign false-refusal controls), `redact_pii`
(emails/phones/long digit runs), `check_links` (IP/punycode/too-long),
`check_scam` (fee, urgency, free-mail recruiter, messenger contact,
domain mismatch; threshold 2). 16 new eval cases incl. benign controls.

Status: done. Gates 2026-11-23: ruff clean, pyright 0 errors, pytest 103
passed, eval 40/40 vs baseline v3.

### Decisions (Phase 3)

- Unknown mail senders already pass the sender prefilter; `detect_platform`
  now returns only known platforms or `"unknown"` (no more `"company"`).
- Scam domain-mismatch skips trusted ATS/platform hosts and matches on
  company first-words minus stopwords, so legit board links never fire it; a
  single weak signal stays below threshold (flag needs >= 2).
- Names are NOT redacted (too noisy); the pipeline forwards structured
  fields, not raw bodies, to hosted LLMs.
- Shared URL regex promoted to public `URL_RE` (`links` reuses it).

## Phase 4 — LLM pipeline

Scope: `resume` loader (example-validated, citable IDs) + deterministic
`check_card` (unknown IDs, gap-with-IDs, reword-without-IDs all block),
`Profile` + heuristic `score_posting` (skill/role/location/deadline, 0-100
with digest-ready reasons), `run_pipeline` (guardrails -> classify ->
extract -> scam screen -> match -> suggest; retry-once then safe skip;
quarantine/flag/violations paths; persists score + `Suggestion`),
`FallbackClient` / `TokenBudget` / `CircuitBreaker`, `build_client` factory,
`run-worker` pipeline stage (skipped when provider is `fake` or files miss).

Status: done. Gates 2026-11-23: ruff clean, pyright 0 errors, pytest 124
passed, eval 49/49 vs baseline v4.

### Decisions (Phase 4)

- Circular import (`agent` <-> `llm.factory` via eager `__init__`s) resolved
  by keeping `llm/__init__` light; factory imported by module path.
- LLM score is the stored score; heuristic rank is appended as an explained
  second opinion, not averaged in.
- Fabrication violations block the suggestion entirely (no partial store).
- `TokenBudget` exhaustion surfaces as safe `skipped`, never a crash; the
  worker stops the stage on exhaustion.
- `Posting` gained `sender`/`body` columns so the pipeline sees real text
  without lossy re-derivation; `NormalizedPosting` carries them end to end.

## Phase 5 — Digest + notifications

Scope: `build_digest` (Closing soon / New today / Best matches with
tailoring notes / Flagged suspicious / Needs your approval), HTML+text
renderers (all content escaped), no-resend rule via `digests_sent` tokens
(deadline/status change re-sends), `SmtpNotifier` (recipient lock, daily cap
shared via `digests_sent`, outbound secret scan, refusal vs transport error
split), approval tokens (random, hashed, expiring, single-use, POST-only
contract), `generate_tailored_resume` executor note, WhatsApp stub that
raises, `build-digest` CLI (`--kind`, `--dry-run`), 10 new eval cases.

Status: done. Gates 2026-11-23: ruff clean, pyright 0 errors, pytest 137
passed, eval 59/59 vs baseline v5.

### Decisions (Phase 5)

- Urgent mail is one combined digest per run (`urgent:<date>`), not one mail
  per posting; empty digests are never sent nor recorded.
- `check_sendable` counts `DigestSent` rows, so digests + urgent share one
  daily cap without a new table.
- `verify-then-decide` re-reads the row inside `decide()`; the GET page never
  mutates, only POST decides (scanner-safe).
- Real sender-domain bug found by the new evals: edge-trimming removed
  internal dots (`gmail.com` -> `gmailcom`), silently disabling the
  free-mail-recruiter signal. Fixed with edge-only trim; regression covered
  by `scm-01`/`pipe-06`.
- PII redaction runs before the scam screen, so phone-based messenger signals
  may not fire on redacted text — other signals (fee, free-mail) still
  quarantine; documented, not widened.

## Phase 6 — Dashboard

Scope: FastAPI + Jinja pages (board with status/scam filters, posting
detail with tailoring card, approvals list + confirm, audit tail, eval
metrics), single-user login (SHA-256 digest, HMAC session cookie, login
rate limit), double-submit CSRF on all POSTs, security headers
(nosniff/DENY/no-referrer/tight CSP), bearer token links need no login,
GET never mutates, `seed-demo` (DEMO_MODE-only, idempotent synthetic
rows), `serve` via uvicorn. Smoked live: `seed-demo` -> `build-digest
--dry-run` -> `serve` answered 303/login page correctly.

Status: done. Gates 2026-11-23: ruff clean, pyright 0 errors, pytest 148
passed, eval 59/59 vs baseline v5 (no new eval tasks; dashboard covered by
13 TestClient tests).

### Decisions (Phase 6)

- Dashboard review links use `/approvals/by-id/{id}` (session-authenticated);
  raw tokens live only in email links since only hashes are stored.
- `_execute_plan` supports `generate_tailored_resume` (writes under
  `data/private/`); unknown kinds record approval without effect.
- `seed-demo` exits 1 with DEMO_MODE off rather than risk polluting a real DB.
- Approval POST for email recipients validates the CSRF double-submit
  cookie, which the valid-token GET (read-only) hands them; scanners only
  GET and can never POST, so execution stays scanner-safe.

## Phase 7 — Deployment

Scope: `Dockerfile` (3.12-slim, `--frozen` uv install; lockfile carries
per-version forks so 3.12 resolves cleanly), `render.yaml` (free web
service, Postgres via external `DATABASE_URL`), `worker.yml` (twice-daily
cron: sync + morning/evening + urgent digests; Gmail token via secret),
`eval-nightly.yml` (suite + `--live-smoke` drift check + artifact),
`pages.yml` (report to GitHub Pages), PR metrics comment job in `ci.yml`,
`eval --report-html/--report-md/--live-smoke`, full README setup guide
(Gmail/IMAP/SMTP/LLM/boards/profile/resume/dashboard/Render/secrets),
final threat-model review.

Status: done. Final gates (2026-11-23, all observed): ruff clean,
pyright 0 errors, pytest 150 passed, eval 59/59 vs baseline v5, secret
scan clean (only scanner patterns + synthetic fixtures match), `docker
build` succeeds and `jobguard eval` passes 59/59 inside the image.

### Decisions (Phase 7)

- Render free tier has no bundled Postgres; `DATABASE_URL` takes any hosted
  Postgres (Neon/Supabase free tier) and falls back to ephemeral SQLite.
- The live smoke asserts reachability + schema validity only, on synthetic
  text — full live-model quality grading is recorded as future work.
- PR comment posts on the green path; a red eval fails the check visibly
  instead of commenting.
- Container smoke found a real packaging bug: `jobguard eval` failed in the
  image because `evals/` was never copied. Fixed (image now ships evals +
  fixtures + example YAMLs) and verified 59/59 inside the container.
- A web test executed a tailored-resume plan into real `data/private/`
  during the suite; the test now chdirs to tmp. `data/private/` is empty
  again.

### Suggested commits (not pushed)

- `chore: phase 7 deployment (Dockerfile, render, workflows, README)`
- Docs and Phase 1-6 work are uncommitted in the working tree; the folder
  is not a git repo yet — `git init` + commit per phase when ready.

## Phase 8 — Minimalist frontend revamp + real-data hardening (in progress)

Scope: SWELL palette (Ocean Teal/Cloud Grey/Papaya) theme, internship-first
board with search, per-job keyword chips (have/add), resume upload page
(YAML or plain text, round-trips through the loader), Sources page with
sync-now, shared `sources.configure.build_adapters` (CLI + web).

Real-data hardening from the first live sync (24 junk postings, 72
classify_fails): schema examples (not raw JSON-Schema) in prompts +
`response_format: json_object`; Gmail HTML-only fallback; multi-job digest
splitter (LinkedIn View-job-anchored, Indeed title+Company-Location pairs,
salary-line guard); Gmail date cursors (the old message-ID cursor never
worked with `after:`); empty-body drops instead of phantom postings.
Independent edge-case sweep (85 probes): 9 failures, all fixed —
subject-fallback phantoms, surrogate crash, 7-digit over-redaction,
injection near-misses (`disregard obstacles`, `new role descriptions`),
uppercase schemes, deadline-now boundary flake — each with unit + eval
regressions (67 cases total).

Gates 2026-11-23: ruff clean, pyright 0 errors, pytest 166 passed, eval
67/67 vs baseline v7.

### Decisions (Phase 8, so far)

- System prompts show a tiny valid example + exact key list; raw schema
  dumps made GPT-OSS echo the schema as content.
- `parse_alert` subject-fallback requires an apply link; linkless labelless
  mail yields nothing (classification is the second net, not the first).
- Injection heuristics favor precision (qualified objects, agentive
  anchors); the pipeline quarantine path stays heuristics-only for now.
- Digest "closing soon" has a 1-hour past-deadline grace; no-resend tokens
  prevent repeats.
- Live 120B findings: temperature=0 + json_object + example prompts parse
  reliably; failures were (a) Groq 429 bursts — fixed with a 3s inter-item
  gap plus bounded retries honoring Retry-After, and (b) tracking-pixel
  junk prefixes causing `{}` punts — fixed by stripping empty-text markdown
  links and truncating over-long spaceless tokens pre-redaction.
- Groq free tier here: 1000 req/~3h, tight per-minute RPM; the worker is a
  guest on that budget, so backfills converge over runs rather than one shot.
- 2026-11-23: sources expanded 1 -> 9 (7 verified boards: HackerRank,
  Workato, Arcesium, Merkle Science, Hevo Data, FamPay, Drivetrain + WWR
  programming RSS + Gmail). Rejected: Lever EU endpoints (payu/aidash 404),
  RemoteOK RSS (404 from here). Boards verified live before wiring.

All phases complete. Awaiting your next direction.
