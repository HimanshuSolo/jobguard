# JobGuard architecture

> Phase 1 scope: foundations only (config, store, LLM interface, policy
> registry, eval harness, CLI skeleton). Later phases fill the STUB packages.

## Runtime topology (target, SPEC section 2)

```
GitHub Actions (scheduled worker, stateless, idempotent, catch-up)
  sources -> ingestion -> guardrails -> agent (LLM pipeline)
    -> policy -> store -> digest -> notify

Render free web service (Phase 6)
  FastAPI + Jinja/HTMX dashboard, signed-link approval endpoint.
  Reads the same DB.

DB: SQLAlchemy. SQLite locally, Postgres in cloud via DATABASE_URL.
```

## Module map (final — all phases)

- `jobguard.guardrails` — `strip_hidden_text`, `scan_injection`,
  `redact_pii`, `check_links`, `check_scam` (threshold 2; flagged items are
  never recommended).
- `jobguard.llm` — protocol + OpenAI-compat adapter + Fake + record/replay
  + `factory.build_client` (breaker -> fallback -> replay composition).
- `jobguard.agent` — `run_pipeline` (guardrails -> classify -> extract ->
  scam screen -> match -> suggest; retry-once then safe skip), `rank`
  heuristic, `TokenBudget`, `CircuitBreaker`, `FallbackClient`.
- `jobguard.resume` — YAML loader + deterministic `check_card` (violations
  block the suggestion).
- `jobguard.policy` — `registry.evaluate` + `approvals` (hashed, expiring,
  single-use tokens; POST-only decide; `generate_tailored_resume` executor).
- `jobguard.digest` — builder (Closing soon / New today / Best matches /
  Flagged / Approvals), HTML+text renderers, no-resend via `digests_sent`.
- `jobguard.notify` — `SmtpNotifier` (recipient lock, shared daily cap,
  outbound secret scan), WhatsApp stub (raises).
- `jobguard.web` — FastAPI dashboard (board/detail/approvals/audit/metrics,
  HMAC sessions, double-submit CSRF, rate-limited login, security headers),
  `seed_demo` (DEMO_MODE-only).
- `jobguard.cli` — `init-db | run-worker | build-digest | eval | serve |
  seed-demo`; `eval --report-html/--report-md/--live-smoke` for Pages, PR
  comments, and the nightly drift check.

## Module map (implemented through Phase 2)

- `jobguard.sources` — `SourceItem`/`SourceAdapter`, `GmailAlertAdapter` over
  `GmailApiMailSource` (readonly) or `ImapMailSource` (readonly, no STORE),
  per-sender `parse_alert` templates, `BoardFeedAdapter` (Greenhouse
  `boards-api.greenhouse.io/v1/boards/{token}/jobs` with `title` /
  `location.name` / `absolute_url`; Lever `api.lever.co/v0/postings/{slug}` top
  array with `text` / `categories.location` / `hostedUrl` / `descriptionPlain`),
  `RssAdapter` (dates normalized to ISO), `fetch_url_safe` (allowlist,
  public-IP-only DNS, size/timeout caps, redirect confinement).
- `jobguard.ingestion` — `normalize_item`, `canonical_key` (company | role |
  location | normalized URL, tracking params stripped), two-pass
  `merge_duplicates` (`seen_on` union), keyword/sender `Prefilter`,
  idempotent `sync_source` (`emails_seen` + `run_state`, fail-safe per adapter).
- `jobguard.cli` — `init-db`, `eval`, and `run-worker` work.

## Module map (Phase 1: implemented)

- `jobguard.config` — `Settings` (pydantic-settings, `.env`), `DEMO_MODE`
  guard via `require_live_credentials()`.
- `jobguard.store` — SQLAlchemy models (`emails_seen`, `raw_postings`,
  `postings`, `suggestions`, `pending_actions`, `digests_sent`, `audit_log`,
  `run_state`) + `get_engine` / `init_db` / `session_factory`.
- `jobguard.llm` — `LLMClient` protocol + `LLMResponse`; `OpenAICompatClient`
  (Groq/OpenRouter/Ollama, sync httpx, bounded timeout); `FakeClient`
  (deterministic canned rules); `RecordReplayClient` (JSONL record/replay);
  `jobguard.llm.schemas` (Classification, PostingExtraction, MatchResult,
  TailoringCard/TailoringSuggestion, `parse_strict`).
- `jobguard.policy` — `TOOLS` registry with risk levels, `evaluate(ctx)` ->
  `allow | needs_approval | deny`. Deterministic code; never consults an LLM.
- `jobguard.cli` — `init-db` and `eval` work; `run-worker`, `build-digest`,
  `serve`, `seed-demo` are marked stubs until their phase.
- `evals/` — runner (dispatch by `task`, `{{OWNER_EMAIL}}` substitution),
  metrics (`summarize`, `compare_with_baseline`), report (Markdown + HTML).

## Module map (stubs; phase in brackets)

`sources` (2), `ingestion` (2), `guardrails` (3), `agent` (4), `resume` (4),
`digest` (5), `notify` (5), `web` (6). Each `__init__.py` names its planned
contents so later phases implement against this map.

## Key data flows (to be wired in later phases)

1. Worker fetch: `run_state` cursor -> adapter `fetch(since)` -> `raw_postings`
   + `emails_seen` (idempotent catch-up, Phase 2).
2. Normalize/dedupe: `canonical_key` merge, `seen_on` union (Phase 2).
3. Guardrails -> LLM pipeline -> policy gate -> `postings`/`suggestions` /
   `pending_actions` (Phases 3-4).
4. Digest builder reads DB, checks `digests_sent`, sends via `Notifier`
   behind the send_email policy (Phase 5).
5. Approvals: signed expiring single-use token links; POST-only execution;
   hashes only in `pending_actions` (Phase 5); dashboard surfaces in Phase 6.

## Decisions carried forward

- Sync `LLMClient.complete()`; async only if a phase proves it necessary.
- JSON columns for list/blob fields keep models backend-agnostic.
- Eval cases are additive per phase (`data/evals/<phase>.jsonl`); the runner
  aggregates all files and the baseline is re-cut deliberately, never to make
  red green.
