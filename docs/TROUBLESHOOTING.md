# Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Gmail rejected the login` | wrong/revoked app password, or 2-Step Verification off | Create a new app password; update `GMAIL_APP_PASSWORD`. |
| Alerts land in spam | new sender pattern | Mark "Not spam" / add the sender to contacts in both inboxes. |
| `Save state` step failed | push to `monitor-state` rejected | Re-run; check Settings → Actions → Workflow permissions isn't blocking the job's `contents: write`. |
| Schedule shows "disabled" | 60-day inactivity rule for public repos | Actions → workflow → *Enable workflow*. |
| Every run is `NOT_AUTHORIZED` | Gate 1 switch off | Set variable `NYRR_COLLECTION_AUTHORIZED=true` (only after authorization). |
| Runs succeed, never any e-mail | `ALERTS_ENABLED` not `true`, or no qualifying opening | Check variable; query `shifts` for `AVAILABLE_VERIFIED`. Openings while alerts were off alert once when switched on. |
| Exit code 2, `configuration_invalid` | missing/invalid secret | Job log lists which variable; `python scripts/validate_config.py`. |
| `connection failed … Network is unreachable` / IPv6 | direct Supabase host used | Use the **Session pooler** URL. |
| `password authentication failed for user "nyrr_monitor_app"` | pooler needs `nyrr_monitor_app.<project_ref>` as user | Use the URL printed by `initialize_database.py`. |
| `permission denied for schema nyrr_monitor` | 002 grants not applied | Re-run `initialize_database.py`. |
| Resend `422 validation_error` / domain not verified | sender domain unverified or gmail sender | Verify the domain in Resend; use `alerts@<that domain>`. |
| Resend `401/403` | wrong/revoked key | New sending-access key → update secret. |
| Health e-mail `PARSER_CHANGE` | NYRR changed markup | See OPERATIONS → Recovery. No false alerts are possible from unparseable pages. |
| Health e-mail `ACCESS_DENIED`; runs `NOT_AUTHORIZED` with "collection paused" | NYRR answered 401/403; the block persists across runs | Do not work around it. Contact NYRR; once resolved run `python scripts/check_health.py --clear-access-block`. |
| `SKIPPED_LOCKED` repeatedly | a run is hanging, or clock skew | Check the previous job; lease expires in 12 min. |
| `MISSED_INTERVALS` | GitHub schedule delays (common at :00) | Informational unless persistent; check githubstatus.com. |
| Workflow stopped running mid-month | Actions minutes exhausted (private repo, no payment method) | Add payment method / spending limit, or make repo public. |
| E-mail test refused "previous email test exists" | restart protection | Re-enable workflow and run with `allow_restart=true`. |
| Alert says shift open but NYRR shows full | filled between verification and click (common for popular shifts) | Expected; the e-mail warns. Next run records the closure; a later reopening alerts again. |
