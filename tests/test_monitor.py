"""Full monitoring cycles against a fake NYRR site + real PostgreSQL (spec §16 cases 11–18, 26, 31)."""

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from src.monitor import run_cycle
from src.nyrr_client import NyrrClient
from src.repository import Repository
from tests.conftest import CALENDAR, MEDICAL, RTD, fixture_text, make_settings, open_shift

BAG = "6ad79c5e3fa07f0001af9bd9"
REG = "https://register.nyrr.org/?event=76745eea47f712e75d62&option=bagcheck"


class Harness:
    def __init__(self, db, site, sender, clock, **settings_overrides):
        self.db, self.site, self.sender, self.clock = db, site, sender, clock
        self.settings = make_settings(**settings_overrides)
        self.repo = Repository(db())
        self.base_rtd = fixture_text("event_race_to_deliver_2026-10-24.html")

    def client(self, s):
        return NyrrClient(base_url="https://prod.nyrr.org", user_agent="test", authorized=s.nyrr_collection_authorized,
                          request_delay=s.request_delay_seconds, max_requests=s.max_requests_per_run,
                          transport=self.site.transport, sleep=self.clock.sleep, monotonic=self.clock.monotonic,
                          now_fn=self.clock)

    def run(self, advance_minutes=15, *, dispatch=True):
        summary = run_cycle(self.settings, connect_fn=lambda url: self.db(), client_factory=self.client,
                            sender_factory=lambda s: self.sender if dispatch else None, now_fn=self.clock,
                            sleep=self.clock.sleep, heartbeat=lambda url, ok: True)
        self.clock.advance(minutes=advance_minutes)
        return summary

    def set_bag(self, status):
        self.site.pages[RTD] = (self.base_rtd if status == "sold-out"
                                else open_shift(self.base_rtd, BAG, REG if status == "available" else None, status))

    def alerts(self):
        return [s for s in self.sender.delivered() if s["subject"].startswith("NYRR 9+1 ALERT")]

    def shift(self):
        return self.repo.get_shift(BAG)


@pytest.fixture
def h(db, site, sender, clock):
    return Harness(db, site, sender, clock)


def test_first_run_full_sweep_no_false_alerts_on_real_pages(h):
    s = h.run()
    assert s.status == "SUCCESS", s.errors
    assert s.counters["full_sweep"] is True
    assert h.site.requests[0] == CALENDAR
    assert MEDICAL not in h.site.requests  # medical-tagged events are never fetched
    assert not any("/2027/" in p for p in h.site.requests)  # 2027 events never fetched
    assert s.counters["requests_made"] == len(h.site.requests) <= 30
    assert s.counters["verified_openings"] == 0
    assert h.alerts() == []  # Race to Deliver / Midnight Run "Limited Availability" trap: no alert


def test_requests_are_paced_at_least_two_seconds_apart(h):
    h.run()
    pacing = [x for x in h.clock.sleeps if x > 0]
    assert len(pacing) >= len(h.site.requests) - 1
    assert all(x >= 2.0 - 1e-9 for x in pacing[: len(h.site.requests) - 1])


def test_11_new_verified_opening_alerts_both_recipients(h):
    h.run()
    h.set_bag("available")
    s = h.run()
    assert s.counters["verified_openings"] == 1
    assert s.counters["notifications_created"] == 2
    alerts = h.alerts()
    assert sorted(a["to"] for a in alerts) == sorted(h.settings.alert_email_to)
    assert h.shift()["classification"] == "AVAILABLE_VERIFIED"


def test_11b_shift_available_on_first_sight_alerts(h):
    h.set_bag("available")
    h.run()
    assert len(h.alerts()) == 2


def test_12_unchanged_opening_does_not_realert(h):
    h.set_bag("available")
    h.run()
    h.run()
    h.run()
    assert len(h.alerts()) == 2


def test_13_confirmed_closure_records_transition_without_email(h):
    h.set_bag("available")
    h.run()
    h.set_bag("sold-out")
    s = h.run()
    assert h.shift()["classification"] == "UNAVAILABLE" and h.shift()["alert_armed"] is True
    assert len(h.alerts()) == 2
    t = h.repo._all("""SELECT previous_classification, new_classification FROM nyrr_monitor.availability_transitions
                       WHERE shift_id = %s ORDER BY detected_at""", (BAG,))
    assert (t[-1]["previous_classification"], t[-1]["new_classification"]) == ("AVAILABLE_VERIFIED", "UNAVAILABLE")
    assert s.status == "SUCCESS"


def test_14_confirmed_reopening_alerts_again(h):
    h.set_bag("available")
    h.run()
    h.set_bag("sold-out")
    h.run()
    h.set_bag("available")
    h.run()
    assert len(h.alerts()) == 4  # two transitions x two recipients
    keys = {a["key"] for a in h.alerts()}
    assert len(keys) == 4


def test_15_temporary_parser_failure_does_not_cause_close_reopen_alert(h):
    h.set_bag("available")
    h.run()
    h.site.pages[RTD] = h.site.pages[RTD].replace("data-registration-options", "data-something-else")
    s = h.run()
    assert s.status == "PARTIAL" and s.counters["parser_errors"] == 1
    assert h.shift()["classification"] == "UNVERIFIED"
    assert "PARSER_ERROR" in h.shift()["reason_codes"]
    h.set_bag("available")  # page healthy again, shift still open
    h.run()
    assert h.shift()["classification"] == "AVAILABLE_VERIFIED"
    assert len(h.alerts()) == 2  # no second alert without a confirmed closure in between


def test_15b_source_error_suspends_and_does_not_realert(h):
    h.set_bag("available")
    h.run()
    h.site.status_overrides[RTD] = 503
    s = h.run()
    assert s.counters["source_failures"] == 1 and h.shift()["classification"] == "UNVERIFIED"
    del h.site.status_overrides[RTD]
    h.run()
    assert len(h.alerts()) == 2


def test_16_missing_shift_becomes_unverified_not_sold_out(h):
    h.run()
    import re
    h.site.pages[RTD] = re.sub(r'<article data-registration-option-id="' + BAG + r'">.*?</article>', "",
                               h.base_rtd, flags=re.S)
    h.run()
    assert h.shift()["classification"] == "UNVERIFIED"
    assert h.shift()["reason_codes"] == ["SHIFT_MISSING"]
    assert h.alerts() == []


def test_17_repeated_identical_cycles_are_idempotent(h):
    h.set_bag("available")
    h.run()
    before = h.repo._one("""SELECT (SELECT COUNT(*) FROM nyrr_monitor.availability_transitions) AS t,
                                   (SELECT COUNT(*) FROM nyrr_monitor.notifications) AS n""")
    for _ in range(3):
        h.run()
    after = h.repo._one("""SELECT (SELECT COUNT(*) FROM nyrr_monitor.availability_transitions) AS t,
                                  (SELECT COUNT(*) FROM nyrr_monitor.notifications) AS n""")
    assert before == after
    assert len(h.alerts()) == 2


def test_18_concurrent_cycle_is_skipped_while_lock_held(h):
    h.repo.acquire_lock("nyrr-monitor", "other-process", now=h.clock(), ttl=timedelta(minutes=12))
    s = h.run(advance_minutes=0)
    assert s.status == "SKIPPED_LOCKED"
    assert h.site.requests == []
    row = h.repo._one("SELECT status FROM nyrr_monitor.monitoring_runs WHERE run_id = %s", (s.run_id,))
    assert row["status"] == "SKIPPED_LOCKED"
    h.clock.advance(minutes=13)  # crashed holder: lease expires
    assert h.run().status == "SUCCESS"


def test_lock_released_after_crash_inside_cycle(h):
    h.site.pages[CALENDAR] = None  # type: ignore[assignment]  # makes handler raise -> crash path

    def boom(request):
        raise RuntimeError("simulated crash")
    h.site.handler = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        h.run(advance_minutes=0)
    assert h.repo._one("SELECT COUNT(*) AS n FROM nyrr_monitor.locks")["n"] == 0


def test_not_authorized_makes_no_requests(db, site, sender, clock):
    h = Harness(db, site, sender, clock, nyrr_collection_authorized=False)
    s = h.run()
    assert s.status == "NOT_AUTHORIZED" and site.requests == []


def test_alerts_disabled_never_emails(db, site, sender, clock):
    h = Harness(db, site, sender, clock, alerts_enabled=False)
    h.set_bag("available")
    h.run()
    assert sender.sent == []
    assert h.shift()["classification"] == "AVAILABLE_VERIFIED"


def test_access_denied_stops_collection_and_flags(h):
    h.site.status_overrides[CALENDAR] = 403
    s = h.run()
    assert s.status == "FAILED"
    assert h.site.requests == [CALENDAR]
    assert "ACCESS_DENIED" in " ".join(s.errors)


def test_calendar_down_still_rechecks_tracked_events(h):
    h.set_bag("available")
    h.run()
    h.site.status_overrides[CALENDAR] = 500
    h.set_bag("sold-out")
    h.run()
    assert RTD in h.site.requests[-3:]
    assert h.shift()["classification"] == "UNAVAILABLE"


def test_26_database_unavailable(clock, site, sender):
    settings = make_settings(database_url="postgresql://nobody:x@127.0.0.1:1/none")

    def connect_fail(url):
        raise psycopg.OperationalError("connection refused")
    with pytest.raises(psycopg.OperationalError):
        run_cycle(settings, connect_fn=connect_fail, now_fn=clock, sleep=clock.sleep)
    assert site.requests == []  # nothing fetched without a database
    assert sender.sent == []


def test_31_monitoring_end_date(db, site, sender, clock):
    clock.now = datetime(2027, 1, 1, 5, 0, 30, tzinfo=UTC)  # 00:00:30 ET Jan 1 2027
    h = Harness(db, site, sender, clock)
    s = h.run()
    assert s.status == "EXPIRED" and s.expired
    assert site.requests == []
    assert [m["subject"] for m in sender.sent] == ["NYRR MONITOR — MONITORING PERIOD ENDED"] * 2
    s2 = h.run()
    assert s2.expired and len(sender.sent) == 2  # summary sent once only
    assert h.repo.get_state("final_summary") is not None


def test_31b_last_minute_of_2026_still_monitors(db, site, sender, clock):
    clock.now = datetime(2027, 1, 1, 4, 59, 0, tzinfo=UTC)  # 23:59 ET Dec 31 2026
    h = Harness(db, site, sender, clock)
    assert h.run().status == "SUCCESS"


def test_production_activation_timestamp_set_once(h):
    h.run()
    first = h.repo.get_state("production_activated_at")
    h.run()
    assert first is not None and h.repo.get_state("production_activated_at") == first


def test_hourly_full_sweep_and_targeted_runs_in_between(h):
    h.run(advance_minutes=15)
    n_first = len(h.site.requests)
    h.site.requests.clear()
    s2 = h.run(advance_minutes=15)
    assert s2.counters["full_sweep"] is False
    assert len(h.site.requests) < n_first  # calendar + limited-availability events only
    # calendar + the five events whose calendar status is available / limited-availability
    assert sorted(p.rsplit("/", 1)[-1] for p in h.site.requests[1:]) == [
        "nyrr-frosty-5k-volunteers", "nyrr-midnight-run-volunteers", "nyrr-ted-corbitt-15k-volunteers",
        "open-run-volunteers", "race-to-deliver-4m-to-benefit-gods-love-we-deliver-volunteers"]


# ---------------------------------------------------------------- regressions from independent review
def test_review1_crash_before_dispatch_then_source_error_still_alerts(h):
    """Opening detected, job dies before e-mailing, next run hits a 503: the alert must not be lost."""
    h.set_bag("available")
    h.run(dispatch=False)  # outbox rows written, nothing sent (simulated crash before dispatch)
    assert h.alerts() == []
    h.site.status_overrides[RTD] = 503
    h.run()  # shift -> UNVERIFIED; pending rows are stale -> SUPPRESSED -> shift re-armed
    assert h.alerts() == [] and h.shift()["alert_armed"] is True
    del h.site.status_overrides[RTD]
    h.run()  # verified again -> alert finally delivered
    assert len(h.alerts()) == 2


def test_review3_access_denied_persists_until_operator_clears(h):
    h.site.status_overrides[CALENDAR] = 403
    assert h.run().status == "FAILED"
    del h.site.status_overrides[CALENDAR]
    h.site.requests.clear()
    s = h.run()
    assert s.status == "NOT_AUTHORIZED" and h.site.requests == []  # no further NYRR contact
    row = h.repo._one("SELECT error_summary FROM nyrr_monitor.monitoring_runs WHERE run_id = %s", (s.run_id,))
    assert "ACCESS_DENIED" in row["error_summary"]
    h.repo.set_state("collection_blocked", {"blocked": False}, now=h.clock())  # operator clears it
    assert h.run().status == "SUCCESS" and h.site.requests


def test_review4_rate_limit_ends_the_run(h):
    h.site.status_overrides[CALENDAR] = 429
    h.site.headers_overrides[CALENDAR] = {"Retry-After": "3600"}
    s = h.run()
    assert h.site.requests == [CALENDAR] and s.status == "FAILED"
    assert "RATE_LIMITED" in " ".join(s.errors)


def test_review5_past_events_are_not_refetched_forever(h):
    import re
    h.run()
    h.site.pages[RTD] = re.sub(r'<article data-registration-option-id="' + BAG + r'">.*?</article>', "",
                               h.base_rtd, flags=re.S)
    h.run()
    assert h.shift()["classification"] == "UNVERIFIED"  # tracked while the event is upcoming
    h.clock.now = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)  # after the Nov 22 event
    h.site.status_overrides[RTD] = 404
    h.site.requests.clear()
    s = h.run()
    assert RTD not in h.site.requests and s.counters["source_failures"] == 0
