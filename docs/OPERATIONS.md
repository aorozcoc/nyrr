# Operations

> Lite setup (v1.1): there is no Supabase SQL editor. To inspect state, download `state/nyrr_monitor.sql.gz`
> from the `monitor-state` branch, or read the job summaries in the Actions tab. `health.yml` checks run
> freshness hourly; the external heartbeat is not used. Where this file mentions Supabase, Resend or
> healthchecks.io, it applies to the full v1.0 setup only.

## Normal behaviour
- `monitor.yml` runs at roughly :00/:15/:30/:45 UTC (GitHub may start it minutes late or skip a slot).
- First run of each hour (by time since the last sweep) fetches every upcoming 2026 non-medical event page
  (~22 requests, ~1 min). Other runs fetch the calendar plus events showing open/limited badges or holding
  tracked shifts (~6 requests).
- Run statuses: `SUCCESS`; `PARTIAL` (some pages failed — affected open shifts are suspended, never assumed
  closed); `FAILED` (calendar unreachable and nothing checked, or NYRR returned 401/403); `SKIPPED_LOCKED`;
  `NOT_AUTHORIZED` (Gate 1 switch off); `EXPIRED` (after 2026-12-31 ET).

## Health
| Status | Meaning (only after activation and before the end date) |
|---|---|
| HEALTHY | last successful run ≤ 25 min ago |
| DEGRADED | 25–45 min, or 2+ consecutive empty 15-min slots |
| CRITICAL | > 45 min; 3 consecutive runs with source failures; NYRR 401/403; page-structure change; alert e-mails failing permanently; database unreachable |

Incident e-mails (`NYRR MONITOR — SYSTEM HEALTH ALERT`) are sent once per unresolved incident; they resolve
automatically when the condition clears. GitHub also e-mails the repo owner when a workflow run fails.
`health.yml` runs hourly, so on its own it notices a stopped monitor 45–105 min late. The required
heartbeat (period 15 min, grace 10 min) e-mails you ~25 min after the last successful run, even if GitHub
itself is down.

## Routine checks
- `python scripts/check_health.py` (read-only) — status, last runs, notification counts.
- Actions tab → job summary of the latest run.
- Supabase SQL editor examples:
  ```sql
  SELECT started_at, status, events_checked, shifts_checked, verified_openings, error_summary
    FROM nyrr_monitor.monitoring_runs ORDER BY started_at DESC LIMIT 20;
  SELECT s.role_name, e.event_name, s.classification, s.availability_status, s.verified_at
    FROM nyrr_monitor.shifts s JOIN nyrr_monitor.events e USING (event_id)
   WHERE s.classification <> 'UNAVAILABLE' ORDER BY s.verified_at DESC;
  SELECT * FROM nyrr_monitor.notifications ORDER BY created_at DESC LIMIT 20;
  ```

## Switches
| To… | Do |
|---|---|
| stop all NYRR requests immediately | set variable `NYRR_COLLECTION_AUTHORIZED` to `false` (or disable `monitor.yml`) |
| keep monitoring but stop opening e-mails | `ALERTS_ENABLED=false` (transitions still recorded) |
| resume after NYRR returned 401/403 | resolve with NYRR first, then `python scripts/check_health.py --clear-access-block` |
| run once now | Actions → NYRR 9+1 Monitor → Run workflow |
| rotate DB password | re-run `scripts/initialize_database.py`; update `DATABASE_URL` secret |
| rotate Resend key | create new key, update secret, delete old key |

## Recovery
- **Stuck lock** (process killed): the lease expires after 12 minutes; the next run proceeds. Never delete
  lock rows while a run is active.
- **E-mail flagged `needs_review`:** fix the cause (sender domain, key), then
  `UPDATE nyrr_monitor.notifications SET delivery_status='FAILED_RETRYABLE', next_attempt_at=now(), needs_review=false WHERE needs_review;`
  — only if the opening is still current; otherwise leave it.
- **Parser incident:** NYRR changed its markup. No alerts are generated from unparseable pages. Capture a new
  page, add it under `tests/fixtures/`, adjust `src/parser.py`, run tests, deploy.

## End of monitoring
After 2026-12-31 23:59 ET the next run records `EXPIRED`, e-mails one final summary, makes no NYRR request,
and a follow-up job disables `monitor.yml`. Also disable `health.yml` manually (Actions → … → Disable
workflow). Data is kept; delete the Supabase project when no longer needed. Nothing transitions to 2027.
