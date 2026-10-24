"""Spec §16 eligibility cases 1–10."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.eligibility import evaluate_shift
from src.models import VolunteerEvent, VolunteerShift
from src.parser import parse_event_page
from tests.conftest import BASE, MIDNIGHT, RTD, T0, fixture_text, open_shift

TZ = ZoneInfo("America/New_York")
REG = "https://register.nyrr.org/?event=76745eea47f712e75d62&option=3d40009ff9613b090383"


def event(**kw):
    base = dict(event_id="2026/race-to-deliver", event_name="Race to Deliver 4M - Volunteers",
                event_date=date(2026, 11, 22), location="Central Park", event_url=BASE + "/races-and-events/2026/x")
    base.update(kw)
    return VolunteerEvent(**base)


def shift(**kw):
    base = dict(shift_id="s1", event_id="2026/race-to-deliver", role_name="Bag Check", start_time=None,
                end_time=None, is_medical=False, plus_one_2026=True, availability_status="available",
                registration_url=REG, source_url=BASE + "/x", verified_at=T0, tags=("9+1",), is_restricted=False)
    base.update(kw)
    return VolunteerShift(**base)


def ev(e, s, **kw):
    return evaluate_shift(e, s, now=kw.pop("now", T0 + timedelta(seconds=30)), tz=TZ, **kw)


def test_01_available_nonmedical_2026_plus_one_shift():
    r = ev(event(), shift())
    assert r.classification == "AVAILABLE_VERIFIED" and r.eligible and r.reason_codes == ["ELIGIBLE"]


def test_02_medical_shift():
    r = ev(event(), shift(is_medical=True, tags=("medical", "9+1 Credit")))
    assert r.classification == "UNAVAILABLE" and not r.eligible and "MEDICAL_ROLE" in r.reason_codes


def test_03_no_plus_one_shift():
    r = ev(event(), shift(plus_one_2026=False, tags=("No +1",)))
    assert r.classification == "UNAVAILABLE" and "NO_PLUS_ONE" in r.reason_codes


def test_04_sold_out_plus_one_shift():
    r = ev(event(), shift(availability_status="sold-out", registration_url=None))
    assert r.classification == "UNAVAILABLE" and r.reason_codes == ["SOLD_OUT"]


@pytest.mark.parametrize("field,code", [("plus_one_2026", "PLUS_ONE_UNCONFIRMED"),
                                        ("is_medical", "MEDICAL_UNCONFIRMED"),
                                        ("is_restricted", "REGISTRATION_UNCONFIRMED")])
def test_05_missing_eligibility_is_never_eligible(field, code):
    r = ev(event(), shift(**{field: None}))
    assert r.classification == "UNVERIFIED" and not r.eligible and code in r.reason_codes


def test_05b_unknown_availability_is_unverified():
    r = ev(event(), shift(availability_status="unknown"))
    assert r.classification == "UNVERIFIED" and "REGISTRATION_UNCONFIRMED" in r.reason_codes


def test_06_missing_registration_url():
    r = ev(event(), shift(registration_url=None))
    assert r.classification == "UNVERIFIED" and r.reason_codes == ["MISSING_REGISTRATION_URL"]
    r2 = ev(event(), shift(registration_url="https://elsewhere.example/?event=1&option=2"))
    assert r2.classification == "UNVERIFIED"


def test_07_wrong_event_year():
    r = ev(event(event_id="2027/joe-kleinerman", event_date=date(2027, 1, 9)), shift(event_id="2027/joe-kleinerman"))
    assert r.classification == "UNAVAILABLE" and "WRONG_YEAR" in r.reason_codes
    # 2027 program text on a 2026-dated page is caught by the parser (plus_one_2026=False)
    html = fixture_text("event_race_to_deliver_2026-10-24.html").replace(
        "November 22, 2026, Sunday at 5:00 AM, Manhattan - Central Park </p>",
        "November 22, 2026, Sunday at 5:00 AM</p><p>This event is +1 eligible for the 2027 9+1 Program.</p>")
    _, shifts = parse_event_page(html, BASE + RTD, T0, tz=TZ)
    assert all(s.plus_one_2026 is False for s in shifts if "9+1" in s.tags)


def test_07b_event_already_passed():
    r = ev(event(event_date=date(2026, 10, 20)), shift())
    assert r.classification == "UNAVAILABLE" and "EVENT_PASSED" in r.reason_codes


def test_08_restricted_volunteer_category():
    r = ev(event(), shift(is_restricted=True, role_name="Volunteer Leaders and Leaders in Training"))
    assert r.classification == "UNAVAILABLE" and "RESTRICTED_ROLE" in r.reason_codes


def test_09_event_level_availability_with_no_qualifying_shift():
    """Real Race to Deliver page: event 'Limited Availability', only a No +1 leaders shift open."""
    e, shifts = parse_event_page(fixture_text("event_race_to_deliver_2026-10-24.html"), BASE + RTD, T0, tz=TZ)
    results = [ev(e, s) for s in shifts]
    assert not any(r.eligible for r in results)
    leader = next(r for r, s in zip(results, shifts, strict=True) if s.availability_status == "available")
    assert set(leader.reason_codes) >= {"NO_PLUS_ONE", "RESTRICTED_ROLE"}
    e2, shifts2 = parse_event_page(fixture_text("event_midnight_run_2026-10-24.html"), BASE + MIDNIGHT, T0, tz=TZ)
    assert not any(ev(e2, s).eligible for s in shifts2)


def test_10_multiple_shifts_with_different_eligibility():
    html = open_shift(fixture_text("event_race_to_deliver_2026-10-24.html"), "6ad79c5e3fa07f0001af9bd9",
                      "https://register.nyrr.org/?event=76745eea47f712e75d62&option=aaaa")
    e, shifts = parse_event_page(html, BASE + RTD, T0, tz=TZ)
    by_role = {s.role_name: ev(e, s) for s in shifts}
    assert by_role["Bag Check"].classification == "AVAILABLE_VERIFIED"
    assert by_role["Course Marshal South"].classification == "UNAVAILABLE"
    assert by_role["Volunteer Leaders and Leaders in Training (NO +1)"].classification == "UNAVAILABLE"
    assert sum(r.eligible for r in by_role.values()) == 1


def test_stale_or_prior_run_evidence_is_never_verified():
    r = ev(event(), shift(), now=T0 + timedelta(minutes=30))
    assert r.classification == "UNVERIFIED" and "STALE_EVIDENCE" in r.reason_codes
    r2 = ev(event(), shift(), run_started_at=T0 + timedelta(seconds=10))
    assert r2.classification == "UNVERIFIED" and "STALE_EVIDENCE" in r2.reason_codes


def test_limited_availability_shift_status_counts_as_open():
    assert ev(event(), shift(availability_status="limited-availability")).eligible


def test_evaluation_is_deterministic():
    a = ev(event(), shift())
    b = ev(event(), shift())
    assert a == b
    assert isinstance(a.evaluated_at, datetime)


def test_review2_multi_day_event_crossing_into_2027():
    """2026/open-run-volunteers runs Jun 2026 - Mar 2027: a shift with unknown date can't be verified."""
    e = event(event_id="2026/open-run-volunteers", event_date=date(2026, 6, 1), event_end_date=date(2027, 3, 31))
    r = ev(e, shift(event_id="2026/open-run-volunteers", start_time=None))
    assert r.classification == "UNVERIFIED" and "SHIFT_DATE_UNCONFIRMED" in r.reason_codes
    jan = datetime(2027, 1, 9, 8, 0, tzinfo=TZ)
    r2 = ev(e, shift(event_id="2026/open-run-volunteers", start_time=jan))
    assert r2.classification == "UNAVAILABLE" and "WRONG_YEAR" in r2.reason_codes
    nov = datetime(2026, 11, 7, 8, 0, tzinfo=TZ)
    assert ev(e, shift(event_id="2026/open-run-volunteers", start_time=nov)).eligible


def test_review2b_multi_day_event_within_2026_still_verifies():
    e = event(event_id="2026/pre-marathon-support", event_date=date(2026, 10, 17), event_end_date=date(2026, 10, 31))
    assert ev(e, shift(event_id="2026/pre-marathon-support", start_time=None)).eligible


@pytest.mark.parametrize("role,desc,expected", [
    ("Volunteer Leaders and Leaders in Training (NO +1)", "Reserved for those in our volunteer leadership program ONLY.", True),
    ("Course Captain", "Captains only.", True),
    ("Bag Check", "By invitation from NYRR staff.", True),
    ("Bag Check", "Only one shift per person. Help staff with baggage.", False),
    ("Fluid Station", "Assist runners and passholders at the fluid station.", False),
    ("Start Corral", "Reserved for returning volunteers.", None),
    ("Bag Check", "Help runners check their bags.", False),
])
def test_review8_restriction_detection(role, desc, expected):
    from src.parser import restriction_status
    assert restriction_status(role, desc) is expected


def test_live_status_slugs_seen_2026_10_24():
    assert ev(event(), shift(availability_status="near-capacity")).eligible
    r = ev(event(), shift(availability_status="completed", registration_url=None))
    assert r.classification == "UNAVAILABLE" and "REGISTRATION_CLOSED" in r.reason_codes
