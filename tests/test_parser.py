from datetime import date
from zoneinfo import ZoneInfo

import pytest

from src.parser import ParserError, classify_tags, event_id_from_url, parse_calendar, parse_event_page, \
    registration_url_is_valid
from tests.conftest import BASE, MEDICAL, MIDNIGHT, RTD, T0, fixture_text, open_shift

TZ = ZoneInfo("America/New_York")


def parse(path, html=None):
    return parse_event_page(html or fixture_text({RTD: "event_race_to_deliver_2026-10-24.html",
                                                  MIDNIGHT: "event_midnight_run_2026-10-24.html",
                                                  MEDICAL: "event_medical_2026-10-24.html"}[path]),
                            BASE + path, T0, tz=TZ)


def test_calendar_lists_all_cards_with_status_and_dates():
    entries = parse_calendar(fixture_text("calendar_2026-10-24.html"), BASE)
    assert len(entries) == 29
    by_id = {e.event_id: e for e in entries}
    rtd = by_id["2026/race-to-deliver-4m-to-benefit-gods-love-we-deliver-volunteers"]
    assert rtd.status == "limited-availability"
    assert rtd.tags == ("9+1 Credit",)
    assert rtd.start.isoformat() == "2026-11-22T05:00:00-05:00"
    assert by_id["2026/tcs-new-york-city-marathon-transportation-volunteers"].tags == ("No +1", "9+1 Credit")
    assert by_id["2026/tcs-new-york-city-marathon-medical-volunteers"].tags == ("Medical", "9+1 Credit")
    assert by_id["2026/pre-marathon-support-volunteers"].end.isoformat() == "2026-10-31T23:59:00-04:00"
    assert {e.year for e in entries} == {2026, 2027}
    assert {e.status for e in entries} == {"available", "sold-out", "closed", "coming-soon", "limited-availability"}


def test_calendar_structure_change_raises():
    with pytest.raises(ParserError):
        parse_calendar("<html><body><main><p>Redesigned</p></main></body></html>", BASE)


def test_event_page_real_race_to_deliver_trap():
    """Event says Limited Availability, but every +1 shift is sold out (live state at discovery)."""
    event, shifts = parse(RTD)
    assert event.event_id == "2026/race-to-deliver-4m-to-benefit-gods-love-we-deliver-volunteers"
    assert event.event_date == date(2026, 11, 22)
    assert event.location == "Central Park"
    assert len(shifts) == 9
    available = [s for s in shifts if s.availability_status == "available"]
    assert len(available) == 1
    leader = available[0]
    assert leader.plus_one_2026 is False and leader.is_restricted is True
    assert leader.registration_url.startswith("https://register.nyrr.org/?event=")
    plus_one = [s for s in shifts if s.plus_one_2026]
    assert len(plus_one) == 8 and all(s.availability_status == "sold-out" for s in plus_one)
    assert all(s.registration_url is None for s in plus_one)
    marshal = next(s for s in shifts if s.role_name == "Course Marshal South")
    assert marshal.start_time.isoformat() == "2026-11-22T06:45:00-05:00"
    assert marshal.end_time.isoformat() == "2026-11-22T10:30:00-05:00"
    assert marshal.is_medical is False and marshal.is_restricted is False


def test_event_page_ignores_related_events_card_and_handles_overnight():
    event, shifts = parse(MIDNIGHT)
    assert len(shifts) == 9  # the "Related Events" race card is not a shift
    assert all(s.event_id == "2026/nyrr-midnight-run-volunteers" for s in shifts)
    amenities = next(s for s in shifts if s.role_name == "Post-Race Amenities")
    assert amenities.start_time.isoformat() == "2026-12-31T22:45:00-05:00"
    assert amenities.end_time.isoformat() == "2027-01-01T02:00:00-05:00"
    assert "2026 9+1 Program" in event.description


def test_medical_shift_has_credit_tag_but_is_medical():
    event, shifts = parse(MEDICAL)
    assert event.tags == ("Medical", "9+1 Credit")
    assert all(s.is_medical is True for s in shifts)
    assert all(s.availability_status == "registration-closed" for s in shifts)


def test_missing_registration_container_is_parser_error():
    html = fixture_text("event_race_to_deliver_2026-10-24.html").replace("data-registration-options", "data-x")
    with pytest.raises(ParserError):
        parse(RTD, html)


def test_missing_dates_is_parser_error():
    html = fixture_text("event_race_to_deliver_2026-10-24.html").replace('"startDate"', '"x"')
    with pytest.raises(ParserError):
        parse(RTD, html)


def test_duplicate_shift_id_is_parser_error():
    html = fixture_text("event_race_to_deliver_2026-10-24.html").replace(
        "6ad79c5e3fa07f0001af9bd3", "6ad79c5e3fa07f0001af9bd1")
    with pytest.raises(ParserError):
        parse(RTD, html)


def test_opened_shift_gets_registration_url():
    html = open_shift(fixture_text("event_race_to_deliver_2026-10-24.html"), "6ad79c5e3fa07f0001af9bd9",
                      "https://register.nyrr.org/?event=abc&option=def")
    _, shifts = parse(RTD, html)
    bag = next(s for s in shifts if s.shift_id == "6ad79c5e3fa07f0001af9bd9")
    assert bag.availability_status == "available"
    assert bag.registration_url == "https://register.nyrr.org/?event=abc&option=def"


def test_off_host_links_are_not_registration_links():
    html = open_shift(fixture_text("event_race_to_deliver_2026-10-24.html"), "6ad79c5e3fa07f0001af9bd9",
                      "https://evil.example.com/?event=abc&option=def")
    _, shifts = parse(RTD, html)
    assert next(s for s in shifts if s.shift_id == "6ad79c5e3fa07f0001af9bd9").registration_url is None


@pytest.mark.parametrize("shift_tags,event_tags,expected", [
    (("9+1",), (), (True, False)),
    (("9+1 Credit",), (), (True, False)),
    (("No +1",), (), (False, False)),
    (("No +1", "9+1 Credit"), (), (False, False)),
    (("medical", "9+1 Credit"), (), (True, True)),
    (("9+1",), ("Medical",), (True, True)),
    ((), (), (None, None)),
])
def test_classify_tags(shift_tags, event_tags, expected):
    assert classify_tags(shift_tags, event_tags) == expected


def test_year_scoped_event_ids_and_url_validation():
    assert event_id_from_url(BASE + "/races-and-events/2027/nyrr-bk-5k-volunteer") == "2027/nyrr-bk-5k-volunteer"
    assert event_id_from_url(BASE + "/get-involved") is None
    hosts = ("register.nyrr.org",)
    assert registration_url_is_valid("https://register.nyrr.org/?event=a&option=b", hosts)
    assert not registration_url_is_valid("http://register.nyrr.org/?event=a&option=b", hosts)
    assert not registration_url_is_valid("https://register.nyrr.org/?event=a", hosts)
    assert not registration_url_is_valid(None, hosts)


def _no_options(html):
    import re
    return re.sub(r'<div data-registration-options="">.*?</div></div></div></div>', "</div></div></div>", html, flags=re.S)


def test_not_yet_open_event_without_shift_list_is_not_a_parser_error():
    """Live 2026-10-24: 'Course Volunteers' (Coming Soon, No +1) has no shift list at all."""
    from src.models import CalendarEntry
    html = _no_options(fixture_text("event_race_to_deliver_2026-10-24.html")).replace(
        "<article", "<div").replace("data-registration-option-id", "data-x")
    entry = CalendarEntry(event_id="2026/race-to-deliver-4m-to-benefit-gods-love-we-deliver-volunteers",
                          event_name="x", event_url=BASE + RTD, start=None, end=None, status="coming-soon",
                          tags=("No +1",))
    event, shifts = parse_event_page(html, BASE + RTD, T0, tz=TZ, calendar_entry=entry)
    assert shifts == [] and event.event_date == date(2026, 11, 22)
    open_entry = entry.model_copy(update={"status": "limited-availability"})
    with pytest.raises(ParserError):  # an event the calendar shows as open must have shifts
        parse_event_page(html, BASE + RTD, T0, tz=TZ, calendar_entry=open_entry)
