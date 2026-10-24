# Security

> Lite setup (v1.1): public repository; secrets `GMAIL_APP_PASSWORD`, `ALERT_EMAIL_FROM`, `ALERT_EMAIL_TO`.
> The database is a throw-away container per job (its password is not a secret); the saved state branch
> is public and contains no addresses or credentials (recipients are pseudonymous keys). Revoke the Gmail
> app password at myaccount.google.com/apppasswords to cut off sending instantly.

## Secrets
- Stored only as GitHub Actions secrets: `DATABASE_URL`, `RESEND_API_KEY`, `ALERT_EMAIL_FROM`,
  `ALERT_EMAIL_TO`, `HEARTBEAT_URL`. None appear in source, fixtures, docs, or git history.
- `.env` is git-ignored; `.env.example` holds placeholders only.
- Logs are JSON and pass through a redactor (Resend keys, DB passwords in URLs, bearer tokens, and every
  configured secret value). Configuration errors name the variable, never its value (tested).
- The Supabase **admin** credential is used once, locally, by `scripts/initialize_database.py`; it is never stored in GitHub.

## Database
- TLS: non-local connections default to `sslmode=require`.
- Runtime role `nyrr_monitor_app`: no superuser, no DDL, no `BYPASSRLS`; `SELECT/INSERT/UPDATE` on monitor
  tables, `DELETE` only on `locks`; 30 s statement timeout. `validate_config.py --check-db` refuses an admin role.
- Dedicated schema `nyrr_monitor` (not exposed by Supabase's REST API) with RLS enabled.
- All SQL is parameterised; only a constant schema prefix is interpolated.

## GitHub
- Private repository; workflow token default `contents: read`. Only the two "disable schedule" jobs get
  `actions: write`, scoped to that job.
- `tests.yml` (runs on PRs) uses no secrets; no workflow uses `pull_request_target`.
- Workflow inputs reach shell only through environment variables (no `${{ }}` interpolation in scripts).
- Third-party actions pinned to full commit SHAs. `persist-credentials: false` on checkout.
- Protect `main`; require approval for fork PR workflows.

## Network behaviour toward NYRR
Same-host GETs only, honest User-Agent with contact, ≥2 s spacing, ≤30 requests/run, bounded retries,
`Retry-After` respected (a 429 or a long Retry-After ends the run), and a 401/403 stops collection in all
later runs until an operator clears it. No proxies, IP rotation, CAPTCHA handling or stealth tooling.
Disabled entirely unless `NYRR_COLLECTION_AUTHORIZED=true`.

## Personal data
Stored: the two recipient addresses (in the outbox, needed to track per-recipient delivery) and public shift
data. Not stored: NYRR credentials, member IDs, page bodies. Recipient addresses are not in the repository.

## Dependencies
Direct dependencies pinned. `tests.yml` runs `pip-audit -r requirements.txt --strict` on every push; a critical
finding fails CI. The audit could not run in the build workspace (no PyPI/OSV access) — the first CI run is
the first audit; record its result in `TEST_RESULTS.md`.

## Outstanding risks
| Risk | Mitigation |
|---|---|
| NYRR Terms of Use (§1 no harvesting) | Gate 1; low volume; stop on block |
| GitHub Actions minutes exhausted on a private repo without a payment method → silent stop | payment method + spending limit, heartbeat |
| NYRR markup change | fail-closed parser + CRITICAL incident |
| GitHub schedule delays/skips | missed-slot detection; no SLA claimed |
| Single e-mail provider | per-recipient retries; permanent failures raise incidents; GitHub failure e-mails as backup channel |
