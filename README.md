# NYRR 9+1 Volunteer Availability Monitor — v1.1 Lite

Checks NYRR's public volunteer pages about every 15 minutes (GitHub Actions, best effort) and e-mails two
recipients when a **2026, +1-eligible, non-medical, individually registerable** volunteer shift opens —
including cancellations that reopen a filled shift. It never registers anyone and stops after
**December 31, 2026 (America/New_York)**.

Lite setup: everything runs in this GitHub repository plus a Gmail account. No database or e-mail
service accounts. Setup steps: `docs/DEPLOYMENT.md`.

## How it decides
An alert requires, for one specific shift on its event page, in the current run:
2026 shift · tagged `9+1` / `9+1 Credit` (not `No +1`) · not medical · not a restricted role (e.g.
leaders-only) · shift status `available` · a live `register.nyrr.org` link · evidence < 20 minutes old.
Event-level "Available / Limited Availability" badges are never enough. Unknown never counts as eligible.

## How it runs
`monitor.yml` (every 15 min): start a PostgreSQL container → restore state from the `monitor-state` branch →
check NYRR → e-mail new openings via Gmail → save state back to the branch.
`health.yml` (hourly): alerts once if no run has completed for 45+ minutes.
`tests.yml`: 180 automated tests on every push (no secrets).

```
src/        application code          tests/    automated tests (NYRR never contacted)
scripts/    state_store.sh, test_email.py, validate_config.py, …
db/         schema                    docs/     deployment, operations, security, troubleshooting
```

## Configuration
| Name | Kind | Value |
|---|---|---|
| `GMAIL_APP_PASSWORD` | secret | 16-character Google app password |
| `ALERT_EMAIL_FROM` | secret | the Gmail address that owns the app password |
| `ALERT_EMAIL_TO` | secret | the two recipient addresses, comma-separated |
| `NYRR_COLLECTION_AUTHORIZED` | variable | `true` to monitor; `false` stops all NYRR requests |
| `ALERTS_ENABLED` | variable | `true` to send opening alerts |
| `NYRR_CONTACT_EMAIL` | variable | optional contact in the User-Agent |
