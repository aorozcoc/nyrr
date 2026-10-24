# Test Results

| Item | Value |
|---|---|
| Executed | 2026-10-23 (ET), build workspace |
| Result | **180 passed, 0 failed, 0 skipped** (v1.1 Lite; 159 from v1.0 + 21 lite tests) |
| Database | real PostgreSQL 16.15 (local throw-away instance), fresh migrated schema per test |
| Python | 3.13.17 locally (pydantic for 3.12 unavailable offline); every file compiles under 3.12.x; CI (`tests.yml`) runs the suite on 3.12 |
| Libraries | httpx 0.28.1, psycopg 3.3.6 (pure-Python mode), pydantic 2.13.5, bs4 4.15.0, pytest 9.1.1, coverage 7.17.0 |
| NYRR | never contacted; captured pages served by an in-process fake site |
| Raw output | `../v0.4_Testing/pytest_output_2026-10-23.txt`, `coverage_report_2026-10-23.txt` |

## Coverage

| Module | Coverage |
|---|---|
| **eligibility.py** | **97%** |
| **repository.py** (state transitions, outbox) | **97%** |
| **notifier.py** (delivery, retries) | **93%** |
| Critical modules combined (spec ≥ 90%) | **95% — gate passed** |
| monitor 88%, parser 90%, health 98%, email_test 95%, config 95%, main 96%, nyrr_client 88% | |
| Total | 94% |

## Spec §16 required cases → tests

| # | Case | Test(s) |
|---|---|---|
| 1 | Available non-medical 2026 +1 shift | `test_eligibility::test_01_…` |
| 2 | Medical shift | `test_02_medical_shift`, `test_parser::test_medical_shift_has_credit_tag_but_is_medical` |
| 3 | No +1 shift | `test_03_no_plus_one_shift` |
| 4 | Sold-out +1 shift | `test_04_sold_out_plus_one_shift` |
| 5 | Missing eligibility | `test_05_missing_eligibility_is_never_eligible` (×3), `test_05b_…` |
| 6 | Missing registration URL | `test_06_missing_registration_url` |
| 7 | Wrong event year | `test_07_wrong_event_year` (+ 2027-program text), `test_07b_event_already_passed` |
| 8 | Restricted category | `test_08_restricted_volunteer_category` |
| 9 | Event-level availability, no qualifying shifts | `test_09_…` on the **real** Race to Deliver and Midnight Run pages; `test_monitor::test_first_run_full_sweep_no_false_alerts_on_real_pages` |
| 10 | Multiple shifts, different eligibility | `test_10_multiple_shifts_with_different_eligibility` |
| 11 | New verified opening | `test_monitor::test_11_…`, `test_11b_…` |
| 12 | Unchanged opening | `test_12_unchanged_opening_does_not_realert` |
| 13 | Confirmed closure | `test_13_confirmed_closure_records_transition_without_email` |
| 14 | Confirmed reopening | `test_14_confirmed_reopening_alerts_again`, `test_end_to_end_opening_lifecycle` |
| 15 | Temporary parsing failure | `test_15_temporary_parser_failure_…`, `test_15b_source_error_…` |
| 16 | Missing shift | `test_16_missing_shift_becomes_unverified_not_sold_out` |
| 17 | Repeated identical cycle | `test_17_repeated_identical_cycles_are_idempotent` |
| 18 | Concurrent cycles | `test_18_concurrent_cycle_is_skipped_while_lock_held`, `test_repository::test_concurrent_identical_observations_record_one_transition` (2 real threads/connections) |
| 19 | Both recipients succeed | `test_notifier::test_19_…` |
| 20 | First recipient fails | `test_20_first_recipient_fails_then_retries_alone` |
| 21 | Second recipient fails | `test_21_second_recipient_fails_permanently_first_preserved` |
| 22 | Provider timeout | `test_22_provider_timeout_reconciles_via_idempotency_key` |
| 23 | Accepted but unconfirmed | `test_23_accepted_is_not_reported_as_delivered` |
| 24 | Duplicate notification attempt | `test_24_duplicate_attempts_do_not_duplicate_emails`, `test_repository::test_duplicate_notification_row_is_rejected` |
| 25 | Retry exhaustion | `test_25_retry_exhaustion_flags_for_review` |
| 26 | Database unavailable | `test_monitor::test_26_database_unavailable`, `test_end_to_end::test_main_exits_1_when_database_unreachable`, `test_cli::test_health_cli_database_unreachable_sends_alert` |
| 27 | Missing secret | `test_end_to_end::test_27_missing_secret` (×4), `test_main_exits_2_on_missing_secret_without_leaking` |
| 28 | Invalid configuration | `test_28_invalid_configuration` (×8) |
| 29 | GitHub workflow execution | `test_29_monitor_workflow`, `test_29_supporting_workflows` (static validation). **Actual cloud execution not yet possible** — no repository exists yet. |
| 30 | Missed monitoring interval | `test_health::test_30_missed_monitoring_intervals`, `test_health_incident_alerts_once_and_resolves` |
| 31 | Monitoring end date | `test_monitor::test_31_…`, `test_31b_last_minute_of_2026_still_monitors`, `test_health::test_end_date_boundary_in_eastern_time` |
| 32 | DST transition | `test_health::test_32_daylight_saving_transition_nov_1_2026` |

Also covered: HTTP policy (retries, Retry-After, 401/403 stop, pacing ≥2 s, 30-request cap, off-host refusal),
least-privilege DB role (DDL and deletes denied), CHECK constraints, rollback atomicity, lease-lock expiry
after crash, stale-evidence suppression, log redaction, Phase 16 lifecycle (4 cycles, 60-min expiry, restart
guard, overlap, recipient isolation), CLI exit codes, and operator scripts.

## Independent review regressions

A separate review agent audited the code against the specification and reported 8 defects (6 reproduced).
Each fix has a regression test; for the HIGH finding the fix was disabled and its test confirmed to fail.

| Finding | Test |
|---|---|
| HIGH — crash before dispatch + transient error silenced an opening | `test_monitor::test_review1_crash_before_dispatch_then_source_error_still_alerts` |
| 2027 shift inside multi-day 2026 event | `test_eligibility::test_review2_…`, `test_review2b_…` |
| 401/403 not persisted across runs | `test_monitor::test_review3_access_denied_persists_until_operator_clears` |
| 429 / long Retry-After didn't end run | `test_monitor::test_review4_rate_limit_ends_the_run`, `test_nyrr_client::test_review4_…` |
| past events re-fetched forever | `test_monitor::test_review5_past_events_are_not_refetched_forever` |
| health cadence | hourly `health.yml` + required heartbeat (configuration; documented) |
| e-mail body changed between retries | `test_notifier::test_review7_email_content_frozen_at_detection` |
| restriction regex false positives | `test_eligibility::test_review8_restriction_detection` (×7) |
| DEGRADED→CRITICAL escalation e-mail | `test_health::test_escalation_from_degraded_to_critical_alerts_once_more` |

## Additional verification performed
- `scripts/initialize_database.py` run twice against a scratch PostgreSQL database: migrations applied,
  role created then rotated, 12 tables / 27 indexes / 10 CHECK constraints present, app role DDL denied.
- `scripts/validate_config.py --check-db` passed with the app role and correctly rejected an admin role.

## Lite-mode tests (v1.1)
`tests/test_lite.py`: Gmail sender success and error classification (no password leakage), Gmail config
validation, recipient pseudonymisation (database holds `recipient-1/2` only while both addresses are
e-mailed), freshness thresholds and one-alert window, freshness CLI, and a real **git state round-trip**:
detect + alert → `state_store.sh save` → drop schema → `restore` → next run sends no duplicate.

## Not yet verified (requires deployment)
- Live NYRR retrieval from GitHub Actions (blocked on Gate 1; also unreachable from the build workspace).
- Real Resend delivery and recipient receipt.
- Scheduled and manual GitHub Actions runs; CI on Python 3.12; `pip-audit` (PyPI/OSV unreachable here).
- Supabase pooler connectivity.

Record those results here after deployment.
