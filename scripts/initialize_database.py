"""Apply migrations and create the least-privilege runtime role (run once, from your own machine).

Usage (PowerShell or bash), with the Supabase *session pooler* connection string of the `postgres` user:
    ADMIN_DATABASE_URL="postgresql://postgres.<ref>:<admin-pw>@aws-0-<region>.pooler.supabase.com:5432/postgres" \
        python scripts/initialize_database.py

What it does
  1. applies db/migrations/001_initial_schema.sql (skipped if already applied)
  2. creates/rotates role nyrr_monitor_app with a newly generated password
  3. applies db/migrations/002_app_role_grants.sql
  4. verifies tables, indexes and constraints, and that the app role can work but cannot run DDL
  5. prints the DATABASE_URL to store as the GitHub Actions secret (shown once, never written to disk)

The admin credential is only used here; it must never be stored in GitHub.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

MIGRATIONS = ROOT / "db" / "migrations"
REQUIRED_TABLES = {"events", "shifts", "monitoring_runs", "availability_transitions", "notifications",
                   "app_state", "locks", "health_incidents", "email_test_runs", "email_test_cycles",
                   "email_test_deliveries"}
REQUIRED_INDEXES = {"events_event_date_idx", "shifts_event_id_idx", "shifts_classification_idx",
                    "shifts_verified_at_idx", "monitoring_runs_started_at_idx", "notifications_delivery_status_idx"}


def applied(conn) -> set[str]:
    exists = conn.execute("SELECT to_regclass('nyrr_monitor.schema_migrations') IS NOT NULL").fetchone()[0]
    if not exists:
        return set()
    return {r[0] for r in conn.execute("SELECT version FROM nyrr_monitor.schema_migrations")}


def app_url(admin_url: str, password: str) -> str:
    p = urlparse(admin_url)
    user = p.username or "postgres"
    # Supabase pooler usernames are "<role>.<project_ref>"
    app_user = "nyrr_monitor_app" + (user[len("postgres"):] if user.startswith("postgres.") else "")
    netloc = f"{quote(app_user)}:{quote(password, safe='')}@{p.hostname}" + (f":{p.port}" if p.port else "")
    local = p.hostname in ("localhost", "127.0.0.1", "::1")
    if "sslmode=" in p.query or local:
        query = p.query
    else:
        query = "sslmode=require" + ("&" + p.query if p.query else "")
    return urlunparse((p.scheme, netloc, p.path, "", query, ""))


def main() -> int:
    admin_url = os.environ.get("ADMIN_DATABASE_URL")
    if not admin_url:
        print("ADMIN_DATABASE_URL is not set (admin connection string; used only by this script).")
        return 2
    local = urlparse(admin_url).hostname in ("localhost", "127.0.0.1", None)
    kwargs = {} if local or "sslmode=" in admin_url else {"sslmode": "require"}
    with psycopg.connect(admin_url, autocommit=True, prepare_threshold=None, **kwargs) as conn:
        done = applied(conn)
        for name in ("001_initial_schema",):
            if name in done:
                print(f"= {name} already applied")
            else:
                conn.execute((MIGRATIONS / f"{name}.sql").read_text())
                print(f"+ applied {name}")

        password = os.environ.get("APP_DB_PASSWORD") or secrets.token_urlsafe(32)
        role_exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'nyrr_monitor_app'").fetchone()
        verb = "ALTER" if role_exists else "CREATE"
        conn.execute(sql.SQL(verb + " ROLE nyrr_monitor_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                             "NOBYPASSRLS PASSWORD {}").format(sql.Literal(password)))
        print(f"+ {'rotated password of' if role_exists else 'created'} role nyrr_monitor_app")
        conn.execute((MIGRATIONS / "002_app_role_grants.sql").read_text())
        print("+ applied 002_app_role_grants")

        tables = {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'nyrr_monitor'")}
        indexes = {r[0] for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'nyrr_monitor'")}
        checks = {r[0] for r in conn.execute(
            """SELECT conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
               WHERE n.nspname = 'nyrr_monitor' AND c.contype = 'c'""")}
        missing = (REQUIRED_TABLES - tables) | (REQUIRED_INDEXES - indexes)
        if missing:
            print(f"! missing objects: {sorted(missing)}")
            return 1
        print(f"✓ {len(tables)} tables, {len(indexes)} indexes, {len(checks)} CHECK constraints present")

    url = app_url(admin_url, password)
    with psycopg.connect(url, autocommit=True, prepare_threshold=None) as app:
        user = app.execute("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
        app.execute("SELECT COUNT(*) FROM nyrr_monitor.shifts")
        try:
            app.execute("CREATE TABLE nyrr_monitor.should_fail (x int)")
            print("! app role can create tables — privileges are too broad")
            return 1
        except psycopg.errors.InsufficientPrivilege:
            pass
        print(f"✓ app role {user[0]} connects, reads monitor tables, superuser={user[1]}, DDL denied")

    print("\nStore this as the GitHub Actions secret DATABASE_URL (it is shown only once):\n")
    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
