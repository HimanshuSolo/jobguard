# JobGuard threat model (final review — all 7 phases complete)

> Every control below is implemented and covered by tests + evals unless
> marked residual. Re-read before changing auth, sending, approvals, or
> any fetch path.

## Assets

- Owner's Gmail metadata + job-alert content (read-only).
- Owner's resume data (`data/private/resume.yaml`, Phase 4+).
- Digest email channel (sending capability to OWNER_EMAIL).
- Approval tokens (capability to execute risky plans).
- Dashboard session (single-user login, Phase 6).

## Threats and controls

| # | Threat | Control | Phase |
| --- | --- | --- | --- |
| 1 | External content (email/feed/JD/link) poisons the agent into unsafe action | Treat all external content as untrusted data; guardrails scan before any LLM call; policy layer is deterministic code, never LLM-judged | 1 (policy), 3 |
| 2 | LLM output drives an unsafe tool call | Every tool gated by `policy.evaluate()`; risky = `PendingAction` + human approval; `submit_application` always denied | 1, 5 |
| 3 | Email sent to wrong recipient / used as exfil channel | `send_email` recipient hard-locked to OWNER_EMAIL; LLM cannot set it; rate + daily caps; secret/PII scan | 1 (rule), 5 |
| 4 | Fabricated resume claims damage owner's credibility | Suggestions must cite `resume.yaml` item IDs; anything else labelled GAP; deterministic fabrication checker | 4 |
| 5 | Credential / PII leak via repo, logs, or CI | `.env` + `data/private/` gitignored; synthetic fixtures only; audit log stores metadata, never content | 1 |
| 6 | Replay / forgery of approval links | Signed, expiring, single-use tokens; store hashes only; POST-only execution (scanner-safe); verify-then-execute | 5 |
| 7 | Malicious URLs (SSRF, private nets, huge bodies) | Domain allowlist; block private/loopback IPs; size + timeout limits | 2, 3 |
| 8 | Live-model nondeterminism hides regressions | Record/replay client; per-push CI uses Fake/replay only; live evals nightly only | 1, 7 |
| 9 | Demo data touches real credentials | `DEMO_MODE=true` makes `require_live_credentials()` raise; enforced at every live-credential entry point | 1 |
| 10 | Dashboard takeover (CSRF, session theft, enumeration) | Single-user login, secure cookies, CSRF on state-changing routes, rate limiting, security headers | 6 |
| 11 | Platform ToS violation (LinkedIn/Naukri/Indeed scraping) | No scraping of prohibitive platforms — alert emails only; respect robots.txt/terms for feeds | 2 |

## Resolved during Phases 2-6 (kept for the record)

- Approval-token crypto and digest sending are done (Phase 5); the
  `send_email` policy rule now guards a real execution path.
- PII redaction and secret scanning are done (Phases 3/5); mailbox content
  reaching hosted LLMs is hidden-text-stripped, injection-screened, and
  PII-redacted first.
