# Deployment — Lite setup (v1.1)

Owner decisions recorded 2026-10-23 22:34 ET:
- Proceed **without NYRR written permission** (owner's informed decision; see `v0.1_Discovery`).
- **Public** GitHub repository `aorozcoc/nyrr` (GitHub Actions free for public repos).
- **No** Supabase, Resend, sender domain, or healthchecks.io accounts. **No** 60-minute e-mail test.
- Alerts sent through **Gmail** with an app password.

What replaces the skipped services:

| Original | Lite |
|---|---|
| Supabase Postgres | PostgreSQL container inside each job; state saved to the `monitor-state` branch after every run (`scripts/state_store.sh`) |
| Resend + verified domain | Gmail SMTP (`smtp.gmail.com:465`) with an app password |
| healthchecks.io heartbeat | hourly `health.yml` checks the age of the last saved state; GitHub e-mails you when a workflow fails |
| 60-minute e-mail test | one optional TEST e-mail on a manual run (checkbox) |

Privacy in a public repo: the saved state holds public NYRR shift data and run history only.
Recipients are stored as `recipient-1` / `recipient-2`; addresses exist only in the `ALERT_EMAIL_TO`
secret. Secrets are never printed (GitHub masks them; the app also redacts them).

## Step 1 — Put the code in the repository

Upload the **contents** of `v1.1_Lite` to the root of `aorozcoc/nyrr`, so the repo has `src/`, `tests/`,
`scripts/`, `db/`, `docs/`, `requirements.txt` … and `.github/workflows/monitor.yml`, `health.yml`,
`tests.yml` at the top level.

- **Easiest:** extract `NYRR_Volunteer_Monitor_v1.1_Lite.zip`, open the extracted `v1.1_Lite` folder, and
  drag everything inside it (including the `.github` folder) onto the repo page → *Add file → Upload files*
  → *Commit changes*.
- If the `.github` folder doesn't upload: on GitHub use *Add file → Create new file*, type the name
  `.github/workflows/monitor.yml`, paste that file's contents, commit; repeat for `health.yml` and `tests.yml`
  (copies are in `v1.1_Lite/github-workflows/` on your computer).

## Step 2 — Gmail app password
1. The sending Gmail account needs 2-Step Verification on.
2. https://myaccount.google.com/apppasswords → create "NYRR monitor" → copy the 16-character password.

## Step 3 — Secrets (repo → Settings → Secrets and variables → Actions → *Secrets* → New repository secret)

| Name | Value |
|---|---|
| `GMAIL_APP_PASSWORD` | the 16-character app password (you enter it; never share it in chat) |
| `ALERT_EMAIL_FROM` | the Gmail address that owns the app password |
| `ALERT_EMAIL_TO` | `davidtorresv@gmail.com,Alejandro.orozco.cuevas@gmail.com` |

## Step 4 — Variables (same page → *Variables* tab → New repository variable)

| Name | Value |
|---|---|
| `NYRR_COLLECTION_AUTHORIZED` | `true` |
| `ALERTS_ENABLED` | `true` |
| `NYRR_CONTACT_EMAIL` | optional — an e-mail NYRR can contact, shown in the request User-Agent |

## Step 5 — Settings
- Settings → Actions → General → Workflow permissions: **Read repository contents** (the monitor job asks
  for write access to the state branch itself).
- Settings → Actions → General → Fork pull request workflows: **Require approval for all outside collaborators**.

## Step 6 — First run
Actions → **NYRR 9+1 Monitor** → *Run workflow* → tick **Also send one TEST notification** → Run.
Check: the job is green; both inboxes got `NYRR MONITOR — TEST NOTIFICATION` (check spam; mark "Not spam");
the job summary shows `status SUCCESS`, `requests_made` ≈ 20–25, `shifts_checked` > 0; a branch
`monitor-state` now exists.

## Step 7 — Confirm the schedule
Within ~30 minutes, the Actions tab should show a run with trigger **schedule** that succeeded. From then on it
runs about every 15 minutes (GitHub may delay or skip some runs; there is no guarantee).

## Notes and limits
- **60-day rule:** GitHub disables schedules in public repos after 60 days without repository activity. The
  state commits from every run should count as activity, but if the schedule ever shows as disabled, click
  *Enable workflow* on the Actions tab.
- **Duplicates:** Gmail has no idempotency keys, and if a job is killed after e-mailing but before saving
  state, the next run can re-send one alert. Rare, and errs toward telling you twice rather than never.
- **Stop everything:** set `NYRR_COLLECTION_AUTHORIZED=false`, or disable the workflow.
- **NYRR blocks access (401/403):** the monitor stops contacting NYRR in all later runs and e-mails a health
  alert. It does not try to get around a block.
- **End of year:** after Dec 31, 2026 (ET) the next run sends a final summary and disables both schedules.
