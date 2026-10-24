# Changelog

## 1.1.0-lite — 2026-10-23
Owner-requested lite deployment (no Supabase, Resend, sender domain, heartbeat, or 60-minute test).
- State: PostgreSQL service container per job; schema dumped to / restored from the `monitor-state` branch.
- E-mail: Gmail SMTP provider (`EMAIL_PROVIDER=gmail`, `GMAIL_APP_PASSWORD`).
- Recipients stored as pseudonymous keys (`recipient-1/2`); addresses only in the secret (public repo).
- Health: hourly freshness check on the state branch (one alert per stoppage) replaces the external heartbeat.
- Manual run option to send one TEST notification. `email-test.yml` not deployed.
- Gate 1 recorded as the owner's decision to proceed without NYRR permission.
- 21 new tests (Gmail, config, pseudonymisation, freshness, git state round-trip): 180 total.

## 1.0.0 — 2026-10-23
Initial implementation (not yet deployed).

- Discovery: CONDITIONAL GO; captured live NYRR pages as fixtures (calendar, Race to Deliver, Midnight Run, Medical).
- Shift-level parser using data attributes + JSON-LD; fails closed on structure change.
- Deterministic eligibility engine (7 checks, unknown ≠ eligible).
- PostgreSQL schema: spec tables + CAS versioning, transactional outbox, lease lock, health incidents, email-test tables; least-privilege role with RLS.
- Alert state machine with `alert_armed` (reopening alerts only after confirmed closure).
- Resend delivery with idempotency keys, per-recipient retries (1 min, 5 min), stale-evidence suppression.
- Health: thresholds, missed-slot detection, incident de-dup, independent `health.yml`, required external heartbeat.
- Automatic end after 2026-12-31 ET with final summary and schedule self-disable.
- Phase 16: 60-minute, 4-cycle email delivery test workflow and results report.
- Safety switches: `NYRR_COLLECTION_AUTHORIZED`, `ALERTS_ENABLED` (both default off).
- 159 tests (including review regressions); critical coverage ≥ 90%.

Fixes from the independent code review (all with regression tests; the most important one verified by
disabling the fix and watching its test fail)
- HIGH: crash before dispatch + transient error could silence a genuine opening forever → re-arm when all of an alert's e-mails were suppressed.
- 2027 shifts inside a multi-day "2026" event could verify → shift date must be 2026; unknown date on a cross-year event → UNVERIFIED.
- 401/403 did not stop later runs → persisted `collection_blocked`; operator clears it.
- 429 / 5xx with long Retry-After did not end the run → run ends.
- Past events with unresolved shifts were re-fetched forever (404 → permanent CRITICAL) → no longer tracked.
- Health thresholds unenforceable at a 2-hour cadence → hourly `health.yml` + required heartbeat.
- E-mail body could change between retries under one Idempotency-Key → content frozen at detection.
- Restriction regex false positives → sentence-scoped, two-tier.
- Minor: final summary e-mail retried; DEGRADED→CRITICAL escalation re-alerts; DB-down alert at most once per day.

Fixes found during verification
- Database initializer forced SSL for local admin URLs; corrected.
- Recipient addresses moved out of code/tests into configuration only.
