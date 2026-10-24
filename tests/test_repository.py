"""Repository behaviour against real PostgreSQL: atomic outbox, CAS transitions, locks, least-privilege role."""

import threading
from datetime import timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest

from src.eligibility import evaluate_shift
from src.parser import parse_event_page
from src.repository import Repository, notification_idempotency_key
from tests.conftest import BASE, MIGRATIONS, RECIPIENTS, RTD, T0, fixture_text, open_shift

TZ = ZoneInfo("America/New_York")
BAG = "6ad79c5e3fa07f0001af9bd9"


def observe(repo, html, now, *, alerts=True, run_id=None):
    import uuid
    run_id = run_id or uuid.uuid4()
    repo.start_run(run_id, started_at=now, trigger="test", scheduled_for=None)
    event, shifts = parse_event_page(html, BASE + RTD, now, tz=TZ)
    outcomes = {}
    with repo.tx():
        repo.upsert_event(event, now=now)
        for s in shifts:
            r = evaluate_shift(event, s, now=now, tz=TZ)
            outcomes[s.shift_id] = repo.apply_observation(s, r, run_id=run_id, now=now, recipients=RECIPIENTS,
                                                          alerts_enabled=alerts)
    return outcomes


def opened():
    return open_shift(fixture_text("event_race_to_deliver_2026-10-24.html"), BAG,
                      "https://register.nyrr.org/?event=76745eea47f712e75d62&option=bag")


def test_new_verified_opening_creates_transition_and_two_outbox_rows_atomically(repo):
    out = observe(repo, opened(), T0)
    bag = out[BAG]
    assert bag.new == "AVAILABLE_VERIFIED" and bag.alert_generated and bag.notifications_created == 2
    rows = repo._all("SELECT * FROM nyrr_monitor.notifications ORDER BY recipient_email")
    assert [r["recipient_email"] for r in rows] == sorted(RECIPIENTS)
    assert all(r["delivery_status"] == "PENDING" for r in rows)
    assert rows[0]["idempotency_key"] == notification_idempotency_key(bag.transition_id, rows[0]["recipient_email"])
    shift = repo.get_shift(BAG)
    assert shift["alert_armed"] is False and shift["state_version"] == 1


def test_alerts_disabled_records_transition_without_outbox_and_alerts_on_activation(repo):
    out = observe(repo, opened(), T0, alerts=False)
    assert out[BAG].new == "AVAILABLE_VERIFIED" and out[BAG].notifications_created == 0
    assert repo.get_shift(BAG)["alert_armed"] is True
    # alerts switched on while the shift is still open -> exactly one alert
    out2 = observe(repo, opened(), T0 + timedelta(minutes=15), alerts=True)
    assert out2[BAG].alert_generated and out2[BAG].notifications_created == 2
    out3 = observe(repo, opened(), T0 + timedelta(minutes=30), alerts=True)
    assert out3[BAG].transition_id is None and out3[BAG].notifications_created == 0


def test_transaction_rollback_leaves_no_partial_state(repo):
    import uuid
    run_id = uuid.uuid4()
    repo.start_run(run_id, started_at=T0, trigger="test", scheduled_for=None)
    event, shifts = parse_event_page(opened(), BASE + RTD, T0, tz=TZ)
    with pytest.raises(RuntimeError), repo.tx():
        repo.upsert_event(event, now=T0)
        for s in shifts:
            repo.apply_observation(s, evaluate_shift(event, s, now=T0, tz=TZ), run_id=run_id, now=T0,
                                   recipients=RECIPIENTS, alerts_enabled=True)
        raise RuntimeError("crash before commit")
    assert repo._one("SELECT COUNT(*) AS n FROM nyrr_monitor.shifts")["n"] == 0
    assert repo._one("SELECT COUNT(*) AS n FROM nyrr_monitor.notifications")["n"] == 0


def test_concurrent_identical_observations_record_one_transition(db):
    """Two processes observe the same opening at the same time: one transition, one pair of emails."""
    import uuid
    setup = Repository(db())
    run_ids = [uuid.uuid4(), uuid.uuid4()]
    for rid in run_ids:
        setup.start_run(rid, started_at=T0, trigger="test", scheduled_for=None)
    event, shifts = parse_event_page(opened(), BASE + RTD, T0, tz=TZ)
    setup.upsert_event(event, now=T0)
    # seed the shift as sold out so both workers race on the same version
    closed_event, closed_shifts = parse_event_page(fixture_text("event_race_to_deliver_2026-10-24.html"),
                                                   BASE + RTD, T0, tz=TZ)
    with setup.tx():
        for s in closed_shifts:
            setup.apply_observation(s, evaluate_shift(closed_event, s, now=T0, tz=TZ), run_id=run_ids[0], now=T0,
                                    recipients=RECIPIENTS, alerts_enabled=True)
    bag = next(s for s in shifts if s.shift_id == BAG)
    result = evaluate_shift(event, bag, now=T0, tz=TZ)
    barrier = threading.Barrier(2)
    outcomes, errors = [], []

    def worker(rid):
        r = Repository(db())
        try:
            barrier.wait()
            with r.tx():
                outcomes.append(r.apply_observation(bag, result, run_id=rid, now=T0, recipients=RECIPIENTS,
                                                    alerts_enabled=True))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(rid,)) for rid in run_ids]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert sum(o.alert_generated for o in outcomes) == 1
    n = setup._one("""SELECT COUNT(*) AS n FROM nyrr_monitor.availability_transitions
                      WHERE shift_id = %s AND new_classification = 'AVAILABLE_VERIFIED'""", (BAG,))["n"]
    assert n == 1
    assert setup._one("SELECT COUNT(*) AS n FROM nyrr_monitor.notifications")["n"] == 2


def test_duplicate_transition_version_is_rejected_by_constraint(repo):
    observe(repo, opened(), T0)
    t = repo._one("SELECT * FROM nyrr_monitor.availability_transitions WHERE shift_id = %s", (BAG,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        repo.conn.execute(
            """INSERT INTO nyrr_monitor.availability_transitions (transition_id, shift_id, new_classification,
               detected_at, run_id, from_version) VALUES (gen_random_uuid(), %s, 'UNAVAILABLE', %s, %s, %s)""",
            (BAG, T0, t["run_id"], t["from_version"]))


def test_duplicate_notification_row_is_rejected(repo):
    observe(repo, opened(), T0)
    n = repo._one("SELECT * FROM nyrr_monitor.notifications LIMIT 1")
    with pytest.raises(psycopg.errors.UniqueViolation):
        repo.conn.execute(
            """INSERT INTO nyrr_monitor.notifications (notification_id, transition_id, recipient_email,
               delivery_status, idempotency_key) VALUES (gen_random_uuid(), %s, %s, 'PENDING', 'other')""",
            (n["transition_id"], n["recipient_email"]))


def test_check_constraints_reject_bad_values(repo):
    observe(repo, opened(), T0)
    with pytest.raises(psycopg.errors.CheckViolation):
        repo.conn.execute("UPDATE nyrr_monitor.shifts SET classification = 'MAYBE' WHERE shift_id = %s", (BAG,))
    with pytest.raises(psycopg.errors.CheckViolation):
        repo.conn.execute("UPDATE nyrr_monitor.notifications SET delivery_status = 'DELIVERED'")
    with pytest.raises(psycopg.errors.CheckViolation):  # AVAILABLE requires url + positive eligibility
        repo.conn.execute("""UPDATE nyrr_monitor.shifts SET classification = 'AVAILABLE_VERIFIED'
                             WHERE shift_id = '6ad79c5e3fa07f0001af9bd1'""")


def test_lease_lock_exclusive_and_expires(repo):
    ttl = timedelta(minutes=12)
    assert repo.acquire_lock("x", "a", now=T0, ttl=ttl)
    assert not repo.acquire_lock("x", "b", now=T0 + timedelta(minutes=5), ttl=ttl)
    # holder crashed without releasing -> lease expires
    assert repo.acquire_lock("x", "b", now=T0 + timedelta(minutes=13), ttl=ttl)
    repo.release_lock("x", "a")  # stale holder cannot release someone else's lock
    assert not repo.acquire_lock("x", "c", now=T0 + timedelta(minutes=14), ttl=ttl)
    repo.release_lock("x", "b")
    assert repo.acquire_lock("x", "c", now=T0 + timedelta(minutes=14), ttl=ttl)


def test_schema_has_required_indexes(repo):
    names = {r["indexname"] for r in repo._all("SELECT indexname FROM pg_indexes WHERE schemaname='nyrr_monitor'")}
    for idx in ["events_event_date_idx", "shifts_event_id_idx", "shifts_classification_idx", "shifts_verified_at_idx",
                "monitoring_runs_started_at_idx", "notifications_delivery_status_idx"]:
        assert idx in names


def test_least_privilege_app_role(db_url, repo):
    """002 grants: the runtime role can do its job but cannot run DDL or delete history."""
    admin = psycopg.connect(db_url, autocommit=True)
    admin.execute("DROP ROLE IF EXISTS nyrr_monitor_app")
    admin.execute("CREATE ROLE nyrr_monitor_app LOGIN PASSWORD 'test-only-pw' NOSUPERUSER NOBYPASSRLS")
    try:
        admin.execute((MIGRATIONS / "002_app_role_grants.sql").read_text())
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        info = conninfo_to_dict(db_url)
        info.update(user="nyrr_monitor_app", password="test-only-pw")
        from src.repository import connect
        app = Repository(connect(make_conninfo(**info)))
        try:
            observe(app, opened(), T0)  # insert/update/select across tables works under RLS
            assert app._one("SELECT COUNT(*) AS n FROM nyrr_monitor.notifications")["n"] == 2
            assert app.acquire_lock("l", "h", now=T0, ttl=timedelta(minutes=1))
            app.release_lock("l", "h")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app.conn.execute("DELETE FROM nyrr_monitor.notifications")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app.conn.execute("DROP TABLE nyrr_monitor.events CASCADE")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app.conn.execute("CREATE TABLE nyrr_monitor.evil (x int)")
        finally:
            app.conn.close()
    finally:
        admin.execute("DROP OWNED BY nyrr_monitor_app")
        admin.execute("DROP ROLE IF EXISTS nyrr_monitor_app")
        admin.close()
