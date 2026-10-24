# Deployment Checklist — Lite (v1.1)

## Decisions (recorded)
- [x] 2026-10-23 22:34 ET — owner decided to proceed **without NYRR written permission**
- [x] Public repository `aorozcoc/nyrr`; Gmail alerts; Supabase/Resend/heartbeat/60-min test skipped by owner

## Build
- [x] 180/180 tests pass locally; critical-module coverage ≥ 90%
- [ ] `Tests` workflow green on GitHub (Python 3.12) — run: ____

## Setup (docs/DEPLOYMENT.md)
- [ ] Code uploaded to repo root, including `.github/workflows/` (monitor, health, tests)
- [ ] Gmail app password created
- [ ] Secrets: GMAIL_APP_PASSWORD, ALERT_EMAIL_FROM, ALERT_EMAIL_TO
- [ ] Variables: NYRR_COLLECTION_AUTHORIZED=true, ALERTS_ENABLED=true
- [ ] Workflow permissions read-only; fork-PR approval required

## Verify
- [ ] Manual run with test e-mail: green — run: ____
- [ ] Recipient 1 received the test e-mail
- [ ] Recipient 2 received the test e-mail
- [ ] `monitor-state` branch exists
- [ ] At least one **schedule** run succeeded — run: ____
- [ ] `NYRR MONITOR — TEST NOTIFICATION` not in spam (marked "Not spam" if it was)
