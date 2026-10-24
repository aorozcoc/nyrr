"""Entry point.

  python -m src.main                     # one monitoring cycle (what monitor.yml runs)
  python -m src.main health              # independent health check (health.yml)
  python -m src.main email-test start --authorized-by NAME [--allow-restart]
  python -m src.main email-test tick
  python -m src.main email-test report [--output EMAIL_TEST_RESULTS.md]

Exit codes: 0 ok/skipped/expired · 1 run failed · 2 configuration invalid.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from datetime import UTC, datetime

from .config import ConfigError, Settings
from .logging_config import configure_logging

log = logging.getLogger("nyrr.main")


def _gh_output(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for k, v in values.items():
                fh.write(f"{k}={v}\n")


def _gh_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def cmd_monitor(settings: Settings) -> int:
    from .monitor import run_cycle

    settings.validate_for(need_db=True, need_email=settings.alerts_enabled)
    summary = run_cycle(settings)
    _gh_output(status=summary.status, expired=str(summary.expired).lower())
    _gh_summary(f"### NYRR monitor run `{summary.status}`\n\n" +
                "\n".join(f"- {k}: {v}" for k, v in summary.counters.items()) +
                ("\n\nErrors:\n" + "\n".join(f"- {e}" for e in summary.errors) if summary.errors else ""))
    return 1 if summary.status == "FAILED" else 0


def cmd_health(settings: Settings) -> int:
    from .health import process_health
    from .monitor import _default_sender
    from .notifier import HEALTH_SUBJECT
    from .repository import Repository, connect

    settings.validate_for(need_db=True, need_email=True)
    sender = _default_sender(settings)
    try:
        try:
            conn = connect(settings.database_url.get_secret_value(), application_name="nyrr-health")
        except Exception as exc:  # database unreachable: alert without DB-based de-duplication
            log.error("health_database_unreachable", extra={"error": type(exc).__name__})
            now = datetime.now(UTC)
            slot = now.strftime("%Y%m%d")  # Resend keeps idempotency keys 24 h -> at most one e-mail per day
            for r in settings.alert_email_to:
                rdigest = hashlib.sha256(r.encode()).hexdigest()[:12]
                sender.send(to=r, subject=HEALTH_SUBJECT, html_body=None,
                            idempotency_key=f"nyrr-health-db-{slot}-{rdigest}",
                            text="OPERATIONAL alert (not a volunteer opening): the health check could not connect "
                                 f"to the monitor database ({type(exc).__name__}). Monitoring is likely stopped.")
            return 1
        try:
            report = process_health(Repository(conn), sender, settings.alert_email_to, now=datetime.now(UTC),
                                    end_date=settings.monitoring_end_date, tz=settings.tz)
        finally:
            conn.close()
    finally:
        sender.close()
    _gh_summary(f"### Health: `{report.worst}`\n" + "\n".join(f"- {i.severity} {i.incident_type}: {i.detail}"
                                                             for i in report.issues))
    _gh_output(health=report.worst)
    return 0


def cmd_freshness(settings: Settings, last_state_epoch: int | None) -> int:
    """Lite-mode stopped-monitor check: no database needed, reads the last state-commit time."""
    from .health import freshness_status
    from .monitor import _default_sender
    from .notifier import HEALTH_SUBJECT

    now = datetime.now(UTC)
    last = datetime.fromtimestamp(last_state_epoch, UTC) if last_state_epoch else None
    status, alert, age = freshness_status(last, now=now, end_date=settings.monitoring_end_date, tz=settings.tz)
    minutes = int(age.total_seconds() // 60) if age else None
    log.info("freshness", extra={"status": status, "age_min": minutes, "alert": alert})
    _gh_output(health=status)
    _gh_summary(f"### Monitor freshness: `{status}` (last saved state {minutes} min ago)")
    if not alert:
        return 0
    settings.validate_for(need_db=False, need_email=True)
    sender = _default_sender(settings)
    text = ("OPERATIONAL alert (not a volunteer opening): the NYRR monitor has not completed a run for "
            f"{minutes} minutes. GitHub may have paused the schedule, or runs are failing. Check the Actions "
            "tab of the repository; new volunteer openings may be missed until it runs again.")
    slot = now.strftime("%Y%m%d%H")
    try:
        for r in settings.alert_email_to:
            sender.send(to=r, subject=HEALTH_SUBJECT, text=text, html_body=None,
                        idempotency_key=f"nyrr-freshness-{slot}-{hashlib.sha256(r.encode()).hexdigest()[:12]}")
    finally:
        sender.close()
    return 1  # failing the job also makes GitHub e-mail the repository owner


def cmd_email_test(settings: Settings, args: argparse.Namespace) -> int:
    from . import email_test
    from .monitor import _default_sender
    from .repository import Repository, connect

    need_email = args.action in ("start", "tick")
    settings.validate_for(need_db=True, need_email=need_email)
    conn = connect(settings.database_url.get_secret_value(), application_name="nyrr-email-test")
    repo = Repository(conn)
    try:
        if args.action == "report":
            text = email_test.build_report(repo, settings.tz)
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(text)
            _gh_summary(text)
            return 0
        sender = _default_sender(settings)
        try:
            if args.action == "start":
                result = email_test.start(repo, sender, settings.alert_email_to, authorized_by=args.authorized_by,
                                          allow_restart=args.allow_restart, tz=settings.tz)
            else:
                result = email_test.tick(repo, sender, settings.alert_email_to, tz=settings.tz,
                                         trigger=settings.run_trigger)
        finally:
            sender.close()
        log.info("email_test_result", extra={"action": result.action, "cycle": result.cycle,
                                             "finished": result.finished})
        _gh_output(finished=str(result.finished).lower(), action=result.action)
        return 0
    except email_test.EmailTestError as exc:
        log.error("email_test_refused", extra={"reason": str(exc)})
        _gh_summary(f"Email test refused: {exc}")
        return 1
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.main")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("monitor")
    sub.add_parser("health")
    fr = sub.add_parser("freshness")
    fr.add_argument("--last-state-epoch", type=int, default=None)
    et = sub.add_parser("email-test")
    et.add_argument("action", choices=["start", "tick", "report"])
    et.add_argument("--authorized-by", default="")
    et.add_argument("--allow-restart", action="store_true")
    et.add_argument("--output", default="EMAIL_TEST_RESULTS.md")
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
    except (ValueError, TypeError) as exc:
        configure_logging()
        log.error("configuration_unreadable", extra={"error": type(exc).__name__})
        return 2
    configure_logging(secrets=settings.secret_values())
    try:
        if args.command == "health":
            return cmd_health(settings)
        if args.command == "freshness":
            return cmd_freshness(settings, args.last_state_epoch)
        if args.command == "email-test":
            if args.action == "start" and not args.authorized_by.strip():
                log.error("email_test_requires_authorized_by")
                return 2
            return cmd_email_test(settings, args)
        return cmd_monitor(settings)
    except ConfigError as exc:
        log.error("configuration_invalid", extra={"problems": exc.problems})
        return 2
    except Exception:
        log.exception("run_crashed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
