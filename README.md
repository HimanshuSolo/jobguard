# JobGuard

Personal, free-to-run agent that aggregates developer / SDE-intern job
postings, ranks them against your profile, flags scams, and emails you a
digest. All phases complete: sources, guardrails, LLM pipeline, digests,
dashboard, deployment.

## Quickstart (local)

```bash
uv sync --extra dev
uv run jobguard init-db
uv run pytest
uv run jobguard eval
```

Every change must keep all gates green (same as `.github/workflows/ci.yml`):

```bash
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest
uv run jobguard eval
```

Try the demo (synthetic data only):

```bash
DEMO_MODE=true DATABASE_URL="sqlite:///./data/demo.db" uv run jobguard seed-demo
DEMO_MODE=true DATABASE_URL="sqlite:///./data/demo.db" uv run jobguard build-digest --dry-run
DEMO_MODE=true DATABASE_URL="sqlite:///./data/demo.db" uv run jobguard serve --port 8000
# open http://127.0.0.1:8000, log in with the dashboard user below
```

## Configuration

Copy `.env.example` to `.env` and fill it in. Never commit `.env` or
`data/private/`, and **never paste credentials into a chat** — set them in
the environment. `DEMO_MODE=true` makes live credentials unusable
(`Settings.require_live_credentials()` raises).

### Gmail reading (readonly)

1. Create a Google Cloud project, enable the Gmail API, and create an
   **OAuth Desktop** client. Download the client secret to
   `data/private/gmail_client_secret.json` (gitignored).
2. First run is interactive: `build_gmail_service()` opens a browser for
   consent with the `gmail.readonly` scope only, then caches the refresh
   token at `data/private/gmail_token.json`.
3. For scheduled jobs, either mount that token file (see
   `.github/workflows/worker.yml`, `GMAIL_TOKEN_JSON` secret) or use the
   IMAP fallback: any provider with IMAP + an app password, configured via
   `IMAP_HOST` / `IMAP_USER` plus `data/private/imap_password.txt`.
   The IMAP source opens the inbox `readonly` and never writes flags.

The agent never sends, deletes, or modifies mail through Gmail — there is
no code path that does.

### Digest sending (separate credentials)

Any SMTP provider (Gmail app password, SendGrid, Mailgun SMTP, etc.):

```bash
SENDER_SMTP_HOST=smtp.example.com SENDER_SMTP_PORT=587
SENDER_SMTP_USER=... SENDER_SMTP_PASSWORD=... SENDER_FROM_ADDRESS=...
SENDER_MAX_PER_DAY=20
```

Recipient is hard-locked to `OWNER_EMAIL`; content is secret-scanned
before sending; the LLM can never set recipients.

### LLM provider (OpenAI-compatible)

Groq, OpenRouter, or local Ollama — anything with a
`POST {base}/chat/completions` endpoint:

```bash
LLM_PROVIDER=openai_compat
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_API_KEY=...
LLM_MODEL=llama-3.3-70b-versatile
# optional fallback: LLM_SECONDARY_BASE_URL / _API_KEY / _MODEL
```

Per-run token budget (`LLM_MAX_TOKENS_PER_RUN`), provider fallback, and a
circuit breaker are built in. `LLM_REPLAY_PATH` + `LLM_REPLAY_MODE` record
or replay calls for deterministic CI.

### What gets listed

`ROLE_LEVEL=intern` keeps only internship / apprentice / trainee developer
titles (SDE 2/3, senior and full-time roles are dropped); `GRAD_YEAR=2027`
drops postings that name only other batches; `LOCATION_SCOPE=india` keeps
India, India-compatible remote and location-less postings. The dashboard and
digest list jobs newest-posted first (board/API publish date, or the alert
email's date).

### Job boards and feeds

```bash
cp data/boards.example.yaml data/boards.yaml  # add Greenhouse/Lever slugs
RSS_FEEDS="careers::https://careers.example.com/feed.xml"
```

No scraping of LinkedIn/Naukri/Indeed — their alert emails are used instead.

### Profile and resume

```bash
cp data/profile.example.yaml data/profile.yaml      # skills, role types, locations
cp data/resume.example.yaml data/private/resume.yaml  # then fill in the REAL resume
```

Resume suggestions cite `resume.yaml` item IDs; anything else is a GAP.
Fabrication violations block the suggestion entirely.

### Dashboard sign-in (Clerk)

1. Create an application at dashboard.clerk.com (email or Google sign-in).
   Under **Configure → Restrictions**, turn sign-ups off or to allowlist-only.
2. Copy **API keys** into `.env` (never into a chat):

   ```bash
   CLERK_PUBLISHABLE_KEY=pk_test_...
   CLERK_SECRET_KEY=sk_test_...
   ```

3. Only `OWNER_EMAIL` can enter (override with `CLERK_ALLOWED_EMAILS`, or
   allow by id with `CLERK_ALLOWED_USER_IDS`). Every request verifies the
   Clerk session JWT (RS256, issuer, expiry); a valid session for any other
   account gets "not allowed". When deployed, set
   `CLERK_AUTHORIZED_PARTIES=https://your-app.example` and add that domain
   in Clerk.

Without `CLERK_PUBLISHABLE_KEY`, the password login below is used.

### Dashboard login (password fallback)

```bash
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD_HASH=<sha256 of a strong password>
DASHBOARD_SECRET_KEY=<long random string>
```

Generate the digest offline (never type the password into a chat):

```bash
python -c "from jobguard.web.auth import hash_password; print(hash_password('...'))"
```

## Running

```bash
uv run jobguard run-worker                  # sync sources + run pipeline
uv run jobguard build-digest --kind morning # or evening | urgent [--dry-run]
uv run jobguard serve --host 127.0.0.1 --port 8000
uv run jobguard eval --report-html evals/report.html
```

## Deployment

- **Worker + digests**: `.github/workflows/worker.yml` (cron, twice daily).
  Required secrets: `DATABASE_URL`, `OWNER_EMAIL`, `LLM_*`, `SENDER_SMTP_*`,
  `IMAP_*` (if used), `GMAIL_TOKEN_JSON` (if Gmail is used).
- **Dashboard**: `render.yaml` (Render free web service, Docker). Set
  `DATABASE_URL` to any hosted Postgres (Neon/Supabase free tier) or leave
  it unset for ephemeral SQLite. Set all `sync: false` env vars in the
  Render dashboard.
- **Eval report**: `.github/workflows/pages.yml` publishes
  `jobguard eval --report-html` output to GitHub Pages; `eval-nightly.yml`
  runs the suite plus a live-provider smoke; CI posts the Markdown table on
  PRs.
- Moving to a VPS later: `Dockerfile` + env config + ORM carry over
  unchanged; point `DATABASE_URL` at local Postgres.

## Layout

- `src/jobguard/` — `config`, `store`, `sources`, `ingestion`,
  `guardrails`, `llm`, `agent`, `resume`, `policy`, `digest`, `notify`,
  `web`, `cli`.
- `tests/` — unit tests (no network; HTTP is mocked, DBs are in-memory).
- `evals/` — runner, metrics, report, `baseline.json` (committed).
- `data/evals/` — synthetic eval cases (`phase1..5.jsonl`, 59 total).
- `data/*.example.yaml` — synthetic shapes; real data lives in gitignored
  `data/private/`.
- `docs/` — `SPEC.md` (the brief), `PROGRESS.md` (phase log),
  `architecture.md`, `threat-model.md`.

## Status

All 7 phases done. See [docs/PROGRESS.md](docs/PROGRESS.md) for the phase
log and decisions, [docs/threat-model.md](docs/threat-model.md) for the
final security review.
