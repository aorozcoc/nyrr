"""Print current monitor health (read-only by default).

    python scripts/check_health.py            # evaluate and print, no emails
    python scripts/check_health.py --notify   # same as the health workflow: open incidents + email once
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import Settings  # noqa: E402
from src.health import evaluate_health, process_health  # noqa: E402
from src.repository import Repository, connect  # noqa: E402


def main(argv: list[str] | None = None, sender=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--clear-access-block", action="store_true",
                    help="resume NYRR collection after a 401/403 block (only once resolved with NYRR)")
    args = ap.parse_args(argv)
    s = Settings.from_env()
    s.validate_for(need_db=True, need_email=args.notify)
    conn = connect(s.database_url.get_secret_value(), application_name="nyrr-check-health")
    try:
        repo = Repository(conn)
        now = datetime.now(UTC)
        if args.clear_access_block:
            repo.set_state("collection_blocked", {"blocked": False, "cleared_at": now.isoformat()}, now=now)
            print("Access block cleared; the next run will contact NYRR again.")
        if args.notify:
            if sender is None:
                from src.monitor import _default_sender
                sender = _default_sender(s)
            report = process_health(repo, sender, s.alert_email_to, now=now, end_date=s.monitoring_end_date, tz=s.tz)
        else:
            report = evaluate_health(repo, now=now, end_date=s.monitoring_end_date, tz=s.tz)
        fmt = (lambda d: d.astimezone(s.tz).strftime("%Y-%m-%d %I:%M %p %Z") if d else "never")
        print(f"Status:            {report.worst}")
        print(f"Last success:      {fmt(report.last_success_at)}")
        print(f"Activated at:      {repo.get_state('production_activated_at') or 'not yet'}")
        print(f"Notifications:     {repo.notification_counts()}")
        for i in report.issues:
            print(f"  [{i.severity}] {i.incident_type}: {i.detail}")
        for r in repo.recent_runs(5):
            print(f"  run {fmt(r['started_at'])} {r['status']:<14} events={r['events_checked']} "
                  f"shifts={r['shifts_checked']} open={r['verified_openings']} errors={r['error_count']}")
        return 0 if report.worst in ("HEALTHY", "INACTIVE") else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
