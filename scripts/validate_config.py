"""Validate configuration without revealing secret values.

    python scripts/validate_config.py            # format checks only
    python scripts/validate_config.py --check-db # also connect and verify schema + privileges
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import Settings  # noqa: E402

NAMES = ["DATABASE_URL", "RESEND_API_KEY", "ALERT_EMAIL_FROM", "ALERT_EMAIL_TO", "APP_ENV", "HEARTBEAT_URL",
         "NYRR_COLLECTION_AUTHORIZED", "ALERTS_ENABLED", "NYRR_CONTACT_EMAIL"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-db", action="store_true")
    args = ap.parse_args(argv)
    import os

    print("Variables (values hidden):")
    for n in NAMES:
        print(f"  {n:28s} {'set' if os.environ.get(n, '').strip() else 'MISSING'}")
    s = Settings.from_env()
    print(f"\nCollection authorized (Gate 1): {s.nyrr_collection_authorized}")
    print(f"Opening alerts enabled (Gate 5): {s.alerts_enabled}")
    print(f"Recipients configured:          {len(s.alert_email_to)}")
    problems = s.problems()
    for p in problems:
        print(f"  ✗ {p}")
    if args.check_db and s.database_url:
        from src.repository import connect
        conn = connect(s.database_url.get_secret_value(), application_name="nyrr-validate")
        try:
            versions = [r["version"] for r in conn.execute(
                "SELECT version FROM nyrr_monitor.schema_migrations ORDER BY version").fetchall()]
            role = conn.execute("SELECT current_user AS u, rolsuper AS su FROM pg_roles "
                                "WHERE rolname = current_user").fetchone()
            print(f"\nDatabase: connected as {role['u']} (superuser={role['su']}), migrations={versions}")
            if role["su"] or role["u"] == "postgres" or role["u"].startswith("postgres."):
                problems.append("DATABASE_URL uses an administrator role; use nyrr_monitor_app")
                print("  ✗ runtime is using an administrator role")
        finally:
            conn.close()
    print("\nOK" if not problems else f"\n{len(problems)} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
