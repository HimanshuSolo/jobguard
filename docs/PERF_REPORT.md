# JobGuard performance report — fast pipeline (Phase 9 work)

Status: Step 1 (diagnosis) complete. All numbers below are measured from the
live database, the code, or previously observed runs. No content, no names,
no secrets — counts and timings only.

## Step 1 — Diagnosis

### Backlog composition (live table state, 2026-11-23)

- Total postings: **293**. With suggestions (scored): **16**. Scam-flagged: **0**.
- Postings carrying any deadline: **0**. Consequence: deadline-proximity
  scoring and the "closing soon" section are currently inert, and the expiry
  check never fires (nothing to compare against).
- Sync-stage drops (never became postings): 89 + 65 prefilter drops observed
  across runs, plus per-run `filtered` counts in `sync.source` rows.
- Terminal non-job outcomes seen in audit (kinds): newsletter, other, plus
  quarantine and scam-flag paths. Exact per-posting partition is approximate:
  `audit_log` rows outlive `postings` rows across the manual table reset, and
  SQLite reuses integer ids, so old audit rows can alias new postings.
  Finding in itself: audit references can dangle after resets (no FK).
- Working estimate for the eligible backlog: **~277 postings without
  suggestions**, of which the recent-window outcome mix is dominated by
  classify-fail retries, then skips (newsletter/other), then a thin tail of
  suggested/extract-fail/match-fail/suggest-fail. Denominator inflators:
  (a) newsletters/promos that parse into postings and cost 1–2 LLM calls
  each before being skipped; (b) decided items (skipped/quarantined/flagged)
  have no persisted terminal state, so every run re-runs them.

### Where the time goes

- Local work per item (measured, synthetic inputs, best-of-20): all local
  stages sum to **1.44 ms** (parse 0.03, split 0.03, normalize 0.00,
  canonical_key 0.007, merge 0.03, prefilter 0.00, strip_hidden 0.61,
  collapse_boilerplate 0.06, scan_injection 0.24, redact_pii 0.20,
  check_links 0.08, check_scam 0.14, rank 0.00, extract_urls 0.01).
  Local work is ~0% of wall time.
- Sleep: a fixed **3.0 s pause per item** in `cli._run_pipeline_stage`,
  placed before `run_pipeline`, unconditional — items that make zero LLM
  calls (quarantine path, pre-existing suggestions skip the loop but every
  *processed* item sleeps) still pay it. Backoff sleeps add 1 s / 2 s per
  retry, plus honored `Retry-After` (capped 60 s).
- Network: per LLM call, observed first-run samples 586 ms–3.7 s,
  clustering ~0.9–1.2 s. No per-call persistence exists (gap closed in
  Step 2.11).
- Run durations from audit history (gap-split runs): 18.0 min (405 rows),
  76.6 min (311 rows), 33.2 min (109 rows, latest, still the active shape).
  Throughput ≈ 4 audit rows/min on the long run.

### Calls, tokens, latency per stage

Deterministic LLM calls per posting (code paths; retry adds at most one
more call per step, HTTP attempts up to 3x per call on transient errors):

| outcome | calls | why |
|---|---|---|
| quarantine | 0 | injection screen runs before any LLM call |
| skipped (not a posting) | 1 | classify only |
| classify_fail | 2 | initial + 1 retry |
| extract_fail / flagged | 3 | 1 classify + 2 extract |
| match_fail | 4–5 | + up to 2 match |
| suggest_fail / suggested | 5–8 | + up to 2 suggest |

Blended from the recent outcome mix: **≈ 2.3 LLM calls per posting**.
Prompt sizes measured from the code (len//4 token estimates): classify
system 307 chars (~76) + user ~1211 chars (~302); extract system 449
(~112); match system 268 (~67); suggest system 454 (~113). Typical outputs
are small JSON (tens to low-hundreds of tokens). True per-call input/output
tokens are reported by the provider on every call but only go to the
filtered console log — not persisted (gap closed in Step 2.11).

### 429s and backoff

No exact 429 count exists anywhere (not in `audit_log`, only in filtered
stderr). Evidence: repeated observed HTTP 429 bursts during live probes;
Groq free tier headers show 1000 requests / ~3 h with tight per-minute
throttling; identical requests flip 200/429 over minutes. Backoff behavior:
1 s, 2 s sleeps per retry (max 3 attempts per call), `Retry-After` honored
up to 60 s. The 429 storm is the dominant throughput limiter, not tokens.

### Sync, order, concurrency

- Every run re-fetches everything: Gmail `after:` date cursor narrows only
  mail; boards and RSS refetch fully; `emails_seen` dedupes writes but not
  the fetch/parse cost. Last full run volumes: boards 243 fetched / 162 new,
  RSS 25 / 22, Gmail 92 / 0 (all seen). 8 `sync.fetch_failed` rows show at
  least one source flapping (retried next run by design).
- Processing is strictly sequential, single-threaded. Order today: intern
  roles first (sort in `_run_pipeline_stage`), then by posting id.
- 3 s pause placement: per item, not per call, in the worker loop.

### Projection for the full eligible backlog (~277 postings)

640 LLM calls ≈ 277 × 2.3; input ≈ 400 tokens × 640 ≈ 250 k tokens;
time ≈ 277 × 3 s sleep + 640 × ~1 s network + backoff ≈ **35–45 min per
pass**, assuming no sustained 429 storm. The per-run token budget caps
progress first (default 20000 ≈ 20 postings; 60000 env override in use).
Convergence is multi-run by design; decided items re-cost 1–2 calls per
run until terminal states are persisted (Step 2.9).

### Hypothesis verdicts

1. Sleep applied to non-LLM items — **CONFIRMED** (unconditional per-item
   sleep in code; quarantine/skip paths still pay it).
2. Inflated denominator — **CONFIRMED** (non-job mail parsed as postings;
   decided items reprocessed every run; 16/293 scored).
3. Four sequential calls per posting — **CONFIRMED as the designed path**;
   measured distribution centers on 1–2 (most items die or stop at classify).
4. Oversized prompts hitting token limits — **REJECTED** as the binding
   constraint (prompts are ~100–400 tokens in; per-minute *request* rate and
   3 h request budget bind first; TPM resets in seconds).
5. Long backoff loops after 429 — **CONFIRMED mechanism** (1 s/2 s + honored
   Retry-After up to 60 s, ×3 attempts per call); exact historical count
   unknowable (gap closed in Step 2.11).
6. No concurrency — **CONFIRMED** (single-threaded loop; one worker alive).
7. Repeated full sync every run — **CONFIRMED** (code; dedupe saves writes,
   not fetch/parse).

## Step 2 — Fixes (in order)
## Step 3 — Tests
## Step 4 — Report and canary plan
