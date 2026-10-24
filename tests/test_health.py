"""Health thresholds, missed intervals (case 30), incident de-duplication, DST (case 32)."""

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from src.health import (CRITICAL, DEGRADED, HEALTHY, INACTIVE, consecutive_missed_slots, floor_slot, ping_heartbeat,
                        process_health, staleness_status)
from src.monitor import is_expired
from src.notifier import render_opening_email
from src.repository import NotificationJob
from tests.conftest import RECIPIENTS, T0, make_settings

TZ = ZoneInfo("America/New_York")
END = date(2026, 12, 31)


@pytest.mark.parametrize("age_min,expected", [(10, HEALTHY), (25, HEALTHY), (26, DEGRADED), (45, DEGRADED),
                                              (46, CRITICAL), (300, CRITICAL)])
def test_staleness_thresholds(age_min, expected):
    act = T0 - timedelta(days=1)
    assert staleness_status(T0 - timedelta(minutes=age_min), now=T0, activated_at=act, end_date=END, tz=TZ) == expected


def test_thresholds_only_apply_while_active():
    assert staleness_status(None, now=T0, activated_at=None, end_date=END, tz=TZ) == INACTIVE
    jan2 = datetime(2027, 1, 2, 12, tzinfo=UTC)
    assert staleness_status(T0, now=jan2, activated_at=T0, end_date=END, tz=TZ) == INACTIVE
    # just activated, no run yet: measured from activation
    assert staleness_status(None, now=T0 + timedelta(minutes=5), activated_at=T0, end_date=END, tz=TZ) == HEALTHY


def test_30_missed_monitoring_intervals():
    act = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
    now = datetime(2026, 11, 1, 12, 7, tzinfo=UTC)
    every_slot = [act + timedelta(minutes=15 * i, seconds=40) for i in range(9)]  # 10:00 .. 12:00
    assert consecutive_missed_slots(every_slot, now=now, activated_at=act) == 0
    # GitHub skipped the 11:30 and 11:45 runs: two consecutive misses (the 12:00 slot is still in progress)
    gap = [t for t in every_slot if floor_slot(t).strftime("%H:%M") not in ("11:30", "11:45")]
    assert consecutive_missed_slots(gap, now=now, activated_at=act) == 2
    # a single skipped run is tolerated
    one = [t for t in every_slot if floor_slot(t).strftime("%H:%M") != "11:45"]
    assert consecutive_missed_slots(one, now=now, activated_at=act) == 1
    # nothing since 11:15, checked at 12:17: 11:30, 11:45 and 12:00 all missed
    stopped = [t for t in every_slot if floor_slot(t) < datetime(2026, 11, 1, 11, 30, tzinfo=UTC)]
    assert consecutive_missed_slots(stopped, now=now + timedelta(minutes=10), activated_at=act) == 3


def test_health_incident_alerts_once_and_resolves(repo, sender):
    act = T0 - timedelta(hours=2)
    repo.set_state("production_activated_at", act.isoformat(), now=act)
    rid = uuid.uuid4()
    repo.start_run(rid, started_at=act, trigger="test", scheduled_for=None)
    repo.finish_run(rid, completed_at=act + timedelta(minutes=1), status="SUCCESS", counters={}, error_summary=None)
    r1 = process_health(repo, sender, RECIPIENTS, now=T0, end_date=END, tz=TZ)
    assert r1.worst == CRITICAL
    types = {i.incident_type for i in r1.issues}
    assert {"STALE_MONITORING", "MISSED_INTERVALS"} <= types
    assert len(sender.sent) == 2 and all(s["subject"] == "NYRR MONITOR — SYSTEM HEALTH ALERT" for s in sender.sent)
    assert "NOT a volunteer opening" in sender.sent[0]["text"]
    process_health(repo, sender, RECIPIENTS, now=T0 + timedelta(minutes=30), end_date=END, tz=TZ)
    assert len(sender.sent) == 2  # same unresolved incident -> no repeat email
    # recovery: runs every 15 minutes again
    for i in range(8):
        rid = uuid.uuid4()
        t = T0 + timedelta(minutes=30 + 15 * i)
        repo.start_run(rid, started_at=t, trigger="test", scheduled_for=floor_slot(t))
        repo.finish_run(rid, completed_at=t + timedelta(minutes=1), status="SUCCESS", counters={}, error_summary=None)
    r3 = process_health(repo, sender, RECIPIENTS, now=T0 + timedelta(minutes=30 + 15 * 7 + 2), end_date=END, tz=TZ)
    assert r3.worst == HEALTHY and repo.open_incidents() == []


def test_three_consecutive_source_failures_flagged(repo, sender):
    repo.set_state("production_activated_at", (T0 - timedelta(hours=1)).isoformat(), now=T0)
    for i in range(3):
        rid = uuid.uuid4()
        t = T0 - timedelta(minutes=30 - 15 * i)
        repo.start_run(rid, started_at=t, trigger="test", scheduled_for=floor_slot(t))
        repo.finish_run(rid, completed_at=t, status="PARTIAL", counters={"source_failures": 1},
                        error_summary="SOURCE_ERROR x")
    r = process_health(repo, sender, RECIPIENTS, now=T0, end_date=END, tz=TZ)
    assert "SOURCE_FAILURES" in {i.incident_type for i in r.issues}


def test_heartbeat_ping_success_and_failure():
    hits = []
    t = httpx.MockTransport(lambda req: hits.append(str(req.url)) or httpx.Response(200))
    assert ping_heartbeat("https://hc-ping.com/abc", ok=True, transport=t)
    assert ping_heartbeat("https://hc-ping.com/abc", ok=False, transport=t)
    assert hits == ["https://hc-ping.com/abc", "https://hc-ping.com/abc/fail"]
    assert ping_heartbeat(None, ok=True) is False
    boom = httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("x")))
    assert ping_heartbeat("https://hc-ping.com/abc", ok=True, transport=boom) is False


def test_32_daylight_saving_transition_nov_1_2026():
    # DST ends 2026-11-01 02:00 EDT -> 01:00 EST; 01:30 happens twice.
    first = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)   # 01:30 EDT
    second = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)  # 01:30 EST
    assert first.astimezone(TZ).tzname() == "EDT" and second.astimezone(TZ).tzname() == "EST"
    # quarter-hour slots are computed in UTC, so the repeated hour has distinct slots
    assert floor_slot(first) != floor_slot(second)
    assert consecutive_missed_slots([first + timedelta(minutes=15 * i) for i in range(5)],
                                    now=second + timedelta(minutes=10), activated_at=first) == 0
    # emails label the zone explicitly so the ambiguous hour is unambiguous to the reader
    job = NotificationJob(
        notification_id=uuid.uuid4(), transition_id=uuid.uuid4(), recipient_email="x@example.com", attempt_count=1,
        idempotency_key="k", shift_id="s", shift_classification="AVAILABLE_VERIFIED", shift_is_latest_transition=True,
        shift_verified_at=second, role_name="Start Volunteer", start_time=first, end_time=second,
        registration_url="https://register.nyrr.org/?event=a&option=b", plus_one_2026=True,
        availability_status="available", event_name="TCS NYC Marathon - Start", event_date=date(2026, 11, 1),
        event_end_date=None, location="Fort Wadsworth", event_url="https://prod.nyrr.org/x", verified_at=second,
        detected_at=second)
    _, text, _ = render_opening_email(job, TZ)
    assert "1:30 AM EDT to 1:30 AM EST" in text
    assert "Verified at:    Sun Nov 1, 2026 1:30 AM EST" in text


def test_end_date_boundary_in_eastern_time():
    s = make_settings()
    assert not is_expired(s, datetime(2027, 1, 1, 4, 59, 59, tzinfo=UTC))   # 23:59:59 ET Dec 31
    assert is_expired(s, datetime(2027, 1, 1, 5, 0, 0, tzinfo=UTC))         # 00:00 ET Jan 1


def test_escalation_from_degraded_to_critical_alerts_once_more(repo, sender):
    act = T0 - timedelta(hours=3)
    repo.set_state("production_activated_at", act.isoformat(), now=act)
    for minutes in (0, 15, 30, 45, 60, 75, 90, 105, 120, 135, 150):
        rid = uuid.uuid4()
        t = act + timedelta(minutes=minutes)
        repo.start_run(rid, started_at=t, trigger="test", scheduled_for=floor_slot(t))
        repo.finish_run(rid, completed_at=t + timedelta(seconds=50), status="SUCCESS", counters={}, error_summary=None)
    last = act + timedelta(minutes=150, seconds=50)
    process_health(repo, sender, RECIPIENTS, now=last + timedelta(minutes=30), end_date=END, tz=TZ)  # DEGRADED
    stale_mails = [s for s in sender.sent if "STALE_MONITORING" in s["text"]]
    assert len(stale_mails) == 2 and "[DEGRADED] STALE_MONITORING" in stale_mails[0]["text"]
    process_health(repo, sender, RECIPIENTS, now=last + timedelta(minutes=40), end_date=END, tz=TZ)  # still DEGRADED
    assert len([s for s in sender.sent if "STALE_MONITORING" in s["text"]]) == 2
    process_health(repo, sender, RECIPIENTS, now=last + timedelta(minutes=50), end_date=END, tz=TZ)  # CRITICAL
    crit = [s for s in sender.sent if "[CRITICAL] STALE_MONITORING" in s["text"]]
    assert len(crit) == 2
