"""Phase 16 — 60-minute email delivery test."""

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from src import email_test
from src.notifier import Outcome
from tests.conftest import RECIPIENTS

TZ = ZoneInfo("America/New_York")


def start(repo, sender, clock, allow_restart=False):
    return email_test.start(repo, sender, RECIPIENTS, authorized_by="owner", allow_restart=allow_restart, tz=TZ,
                            now_fn=clock, sleep=clock.sleep)


def tick(repo, sender, clock):
    return email_test.tick(repo, sender, RECIPIENTS, tz=TZ, now_fn=clock, sleep=clock.sleep)


def test_four_cycles_then_stops(repo, sender, clock):
    t_start = clock()
    r = start(repo, sender, clock)
    assert r.action == "started" and len(sender.sent) == 2
    assert sender.sent[0]["subject"] == "NYRR MONITOR — TEST [1/4]"
    body = sender.sent[0]["text"]
    for line in ["NYRR 9+1 Volunteer Monitor — Email Delivery Test", "Test number: 1",
                 "Cloud execution status: SUCCESS", "Email notification system: TEST MODE",
                 "No actual volunteer opportunity has been detected or verified.", "No action is required.", "EDT"]:
        assert line in body
    clock.advance(minutes=5)
    assert tick(repo, sender, clock).action == "not_due"
    for n in (2, 3, 4):
        clock.now = t_start + timedelta(minutes=15 * (n - 1), seconds=40)  # scheduled job, slightly late
        res = tick(repo, sender, clock)
        assert res.action == "sent" and res.cycle == n
    assert res.finished
    assert [s["subject"][-5:] for s in sender.sent] == ["[1/4]"] * 2 + ["[2/4]"] * 2 + ["[3/4]"] * 2 + ["[4/4]"] * 2
    clock.advance(minutes=15)
    assert tick(repo, sender, clock).action == "no_active_test"
    assert len(sender.sent) == 8
    report = email_test.build_report(repo, TZ)
    assert "Expected executions: 4 · Actual executions: 4" in report
    assert "Accepted by provider: 8" in report and "awaiting recipient confirmation" in report
    assert "not confirmed inbox delivery" in report


def test_window_expiry_ends_test_with_missed_cycles(repo, sender, clock):
    start(repo, sender, clock)
    clock.advance(minutes=61)  # schedule never fired in time
    res = tick(repo, sender, clock)
    assert res.finished and len(sender.sent) == 2
    report = email_test.build_report(repo, TZ)
    assert "Test status: EXPIRED" in report and "Missed scheduled executions: 2, 3, 4" in report


def test_cannot_restart_without_explicit_authorization(repo, sender, clock):
    start(repo, sender, clock)
    with pytest.raises(email_test.EmailTestError):
        start(repo, sender, clock)  # still active
    clock.advance(minutes=61)
    tick(repo, sender, clock)
    with pytest.raises(email_test.EmailTestError):
        start(repo, sender, clock)  # previous test exists
    assert start(repo, sender, clock, allow_restart=True).action == "started"


def test_overlapping_ticks_do_not_duplicate_a_cycle(repo, sender, clock):
    start(repo, sender, clock)
    clock.advance(minutes=15)
    tick(repo, sender, clock)
    clock.advance(minutes=1)
    assert tick(repo, sender, clock).action == "not_due"  # second job right after does nothing
    assert len(sender.sent) == 4


def test_recipient_failure_is_isolated_and_recorded(repo, sender, clock):
    sender.script[RECIPIENTS[1]] = [Outcome.RETRYABLE, Outcome.RETRYABLE, Outcome.RETRYABLE]
    start(repo, sender, clock)
    report = email_test.build_report(repo, TZ)
    assert "Accepted by provider: 1" in report and "Rejected/failed: 1" in report
    assert [s["to"] for s in sender.sent].count(RECIPIENTS[0]) == 1


def test_does_not_touch_production_tables(repo, sender, clock):
    start(repo, sender, clock)
    n = repo._one("""SELECT (SELECT COUNT(*) FROM nyrr_monitor.notifications)
                          + (SELECT COUNT(*) FROM nyrr_monitor.monitoring_runs) AS n""")["n"]
    assert n == 0
