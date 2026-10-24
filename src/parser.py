"""HTML parsing for the NYRR volunteer calendar and event pages.

Only semantic hooks observed during discovery are used (never hashed CSS classes):
  calendar card ......... a[href^="/races-and-events/"][data-race-status]
  shift container ....... [data-registration-options]
  shift ................. article[data-registration-option-id]
  status ................ [data-event-status]  (slug value)
  tags .................. ul > li
  registration link ..... a[href] on an allow-listed host (register.nyrr.org)
  event dates ........... JSON-LD SportsEvent startDate / endDate

Anything structurally unexpected raises ParserError so the caller fails closed.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from .models import CalendarEntry, VolunteerEvent, VolunteerShift

EVENT_PATH_RE = re.compile(r"^/races-and-events/(\d{4})/([a-z0-9][a-z0-9-]*)/?$")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([AaPp])\.?\s*[Mm]\.?")
RANGE_RE = re.compile(
    r"(\d{1,2}:\d{2}\s*[AaPp]\.?\s*[Mm]\.?)\s*[-–—]\s*(\d{1,2}:\d{2}\s*[AaPp]\.?\s*[Mm]\.?)"
)
PROGRAM_YEAR_RE = re.compile(r"(\d{4})\s+9\+1\s+(?:Pass\s+)?[Pp]rogram")

PLUS_ONE_TAGS = {"9+1", "9+1 credit"}
NO_PLUS_ONE_TAGS = {"no +1", "no+1"}
MEDICAL_TAGS = {"medical"}
MEDICAL_KEYWORDS = re.compile(
    r"\b(medical|physician|nurse|nursing|emt|paramedic|athletic trainer|chiropract\w*|"
    r"physical therap\w*|occupational therap\w*|podiatr\w*|pharmac\w*|massage therap\w*)\b",
    re.IGNORECASE,
)
# Explicit restrictions (definitive -> shift is_restricted=True -> UNAVAILABLE). Matches stay within
# one sentence ([^.]) so phrases like "Only one shift per person. Help staff…" do not trigger.
RESTRICTED_STRONG = re.compile(
    r"(\bvolunteer leaders?\b|\bleaders in training\b|\bleadership program\b|\binvitation only\b|"
    r"\bby invitation\b|\bcaptains? only\b|\bleaders? only\b|"
    r"\breserved for\b[^.]{0,80}\b(leaders?|leadership|captains?|staff|invited)\b|"
    r"\bonly\b[^.]{0,40}\b(leaders?|captains?|invited)\b(?![^.]{0,10}\bwelcome))",
    re.IGNORECASE,
)
# Hints that the role may not be open to every member (heuristic -> is_restricted=None -> UNVERIFIED,
# so no alert, but also no "confirmed unavailable").
RESTRICTED_WEAK = re.compile(r"(\breserved for\b|\bgroup entry\b|\bpre-?approved\b|\bapproved volunteers only\b)",
                             re.IGNORECASE)


def restriction_status(role: str, description: str) -> bool | None:
    text = f"{role}. {description}"
    if RESTRICTED_STRONG.search(text):
        return True
    if RESTRICTED_WEAK.search(text):
        return None
    return False


class ParserError(Exception):
    """The page did not have the structure the monitor relies on."""


def _text(node: Tag | None) -> str:
    if node is None:
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


def _norm_tag(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split()).strip()


def event_id_from_url(url: str) -> str | None:
    path = urlparse(url).path
    m = EVENT_PATH_RE.match(path)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else None


# ====================================================================== calendar
def parse_calendar(html: str, base_url: str) -> list[CalendarEntry]:
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.select('a[href^="/races-and-events/"][data-race-status]')
    if not anchors:
        raise ParserError("calendar: no event cards found (page structure changed?)")

    entries: dict[str, CalendarEntry] = {}
    for a in anchors:
        href = a.get("href", "")
        url = urljoin(base_url + "/", href)
        event_id = event_id_from_url(url)
        if event_id is None:
            continue
        card = a.parent
        if not isinstance(card, Tag):
            continue
        status_node = card.select_one("[data-event-status]")
        times = card.select("time[datetime]")
        start = _parse_dt(times[0].get("datetime")) if times else None
        end = _parse_dt(times[1].get("datetime")) if len(times) > 1 else None
        h3 = card.find("h3")
        info = h3.parent if h3 else None
        paragraphs = info.find_all("p", recursive=False) if info else []
        tags = tuple(
            _norm_tag(s.get_text()) for s in (info.select("span") if info else [])
            if s is not status_node and _norm_tag(s.get_text()) and not s.find_parent("h3")
            and s.get("data-event-status") is None
        )
        entries[event_id] = CalendarEntry(
            event_id=event_id,
            event_name=_text(h3) or _text(a),
            event_url=url,
            start=start,
            end=end,
            status=status_node.get("data-event-status") if status_node else None,
            tags=tags,
            location=_text(paragraphs[0]) if paragraphs else None,
            description=" ".join(_text(p) for p in paragraphs[1:]),
        )
    if not entries:
        raise ParserError("calendar: event cards had no recognisable event URLs")
    return list(entries.values())


# ====================================================================== event page
def _json_ld_event(soup: BeautifulSoup) -> dict | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for node in nodes if isinstance(nodes, list) else []:
            if isinstance(node, dict) and node.get("@type") in ("SportsEvent", "Event"):
                return node
    return None


def _parse_clock(value: str) -> time | None:
    m = TIME_RE.search(value)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return None
    hour = hour % 12 + (12 if ampm == "p" else 0)
    return time(hour, minute)


def _shift_times(
    clock_text: str, description: str, day: date | None, tz: ZoneInfo
) -> tuple[datetime | None, datetime | None]:
    if day is None:
        return None, None
    start_clock = _parse_clock(clock_text)
    if start_clock is None:
        return None, None
    start = datetime.combine(day, start_clock, tzinfo=tz)
    end = None
    m = RANGE_RE.search(description)
    if m and _parse_clock(m.group(1)) == start_clock:
        end_clock = _parse_clock(m.group(2))
        if end_clock is not None:
            end = datetime.combine(day, end_clock, tzinfo=tz)
            if end <= start:
                end += timedelta(days=1)  # overnight shift, e.g. 10:45 PM - 2:00 AM
    return start, end


def classify_tags(shift_tags: tuple[str, ...], event_tags: tuple[str, ...]) -> tuple[bool | None, bool | None]:
    """Return (has_plus_one_tag, is_medical_by_tag) from shift + event tags.

    plus-one: True if a 9+1 tag and no "No +1" tag; False if "No +1"; None if no tags.
    medical:  True if a medical tag on shift or event; False if shift has tags but none medical;
              None if the shift has no tags at all (cannot tell).
    """
    lowered = {t.lower() for t in shift_tags}
    ev_lowered = {t.lower() for t in event_tags}
    if lowered & NO_PLUS_ONE_TAGS:
        plus = False
    elif lowered & PLUS_ONE_TAGS:
        plus = True
    else:
        plus = None
    if lowered & MEDICAL_TAGS or ev_lowered & MEDICAL_TAGS:
        medical = True
    elif lowered:
        medical = False
    else:
        medical = None
    return plus, medical


def program_years_mentioned(text: str) -> set[int]:
    return {int(y) for y in PROGRAM_YEAR_RE.findall(text)}


def parse_event_page(
    html: str,
    url: str,
    verified_at: datetime,
    *,
    tz: ZoneInfo,
    monitoring_year: int = 2026,
    registration_hosts: tuple[str, ...] = ("register.nyrr.org",),
    calendar_entry: CalendarEntry | None = None,
) -> tuple[VolunteerEvent, list[VolunteerShift]]:
    event_id = event_id_from_url(url)
    if event_id is None:
        raise ParserError(f"event page URL not recognised: {url}")
    soup = BeautifulSoup(html, "html.parser")

    ld = _json_ld_event(soup)
    start = _parse_dt(ld.get("startDate")) if ld else None
    end = _parse_dt(ld.get("endDate")) if ld else None
    if start is None and calendar_entry is not None:
        start, end = calendar_entry.start, end or calendar_entry.end
    if start is None:
        raise ParserError(f"{event_id}: no machine-readable event date")
    h1 = soup.find("h1")
    name = _text(h1) or (ld or {}).get("name", "").strip()
    if not name:
        raise ParserError(f"{event_id}: no event name")
    location = None
    if ld and isinstance(ld.get("location"), dict):
        location = (ld["location"].get("name") or "").strip() or None

    container = soup.select_one("[data-registration-options]")
    if container is None:
        raise ParserError(f"{event_id}: registration options container missing")

    hero = soup.select_one('[data-block-type="event-hero"]')
    event_tags = tuple(_norm_tag(li.get_text()) for li in hero.select("ul li")) if hero else ()
    if calendar_entry is not None and not event_tags:
        event_tags = calendar_entry.tags
    # Event intro text sits in the block immediately before the filters + options column.
    # Using only that block keeps "Related Events" cards and footers out of the evidence.
    description = ""
    options_column = container.parent
    intro = options_column.find_previous_sibling() if isinstance(options_column, Tag) else None
    if isinstance(intro, Tag):
        description = " ".join(_text(p) for p in intro.find_all("p"))

    start_et = start.astimezone(tz)
    end_et = end.astimezone(tz) if end else None
    multi_day = end_et is not None and end_et.date() != start_et.date()
    event = VolunteerEvent(
        event_id=event_id,
        event_name=name,
        event_date=start_et.date(),
        location=location,
        event_url=url,
        event_end_date=end_et.date() if end_et else None,
        event_status=calendar_entry.status if calendar_entry else None,
        tags=event_tags,
        description=description,
    )

    event_years = program_years_mentioned(description)
    shifts: list[VolunteerShift] = []
    seen: set[str] = set()
    for article in container.select("article[data-registration-option-id]"):
        shift_id = (article.get("data-registration-option-id") or "").strip()
        if not shift_id or shift_id in seen:
            raise ParserError(f"{event_id}: missing or duplicate shift id")
        seen.add(shift_id)
        status_node = article.select_one("[data-event-status]")
        status = (status_node.get("data-event-status") or "").strip().lower() if status_node else ""
        role = _text(article.find("h3"))
        if not role:
            raise ParserError(f"{event_id}/{shift_id}: shift without role name")
        paragraphs = article.find_all("p")
        clock_text = _text(paragraphs[0]) if paragraphs else ""
        desc = " ".join(_text(p) for p in paragraphs[1:])
        tags = tuple(t for t in (_norm_tag(li.get_text()) for li in article.select("ul li")) if t)

        reg_url = None
        for a in article.find_all("a", href=True):
            parsed = urlparse(a["href"])
            if parsed.hostname in registration_hosts:
                reg_url = a["href"]
                break

        has_plus, medical = classify_tags(tags, event_tags)
        if medical is not True and MEDICAL_KEYWORDS.search(role):
            medical = True
        # Explicit 2026 credit: a 9+1 tag on the shift, on a 2026 event, and no text tying
        # this event to a different program year.
        plus_one_2026: bool | None
        if has_plus is None:
            plus_one_2026 = None
        elif has_plus is False:
            plus_one_2026 = False
        else:
            other_years = (event_years | program_years_mentioned(desc)) - {monitoring_year}
            plus_one_2026 = event.event_date.year == monitoring_year and not other_years
        restricted = restriction_status(role, desc)

        day = None if multi_day else start_et.date()
        s_time, e_time = _shift_times(clock_text, desc, day, tz)
        shifts.append(
            VolunteerShift(
                shift_id=shift_id,
                event_id=event_id,
                role_name=role,
                start_time=s_time,
                end_time=e_time,
                is_medical=medical,
                plus_one_2026=plus_one_2026,
                availability_status=status or "unknown",
                registration_url=reg_url,
                source_url=url,
                verified_at=verified_at,
                tags=tags,
                is_restricted=restricted,
                description=desc,
            )
        )
    return event, shifts


def registration_url_is_valid(url: str | None, hosts: tuple[str, ...]) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in hosts:
        return False
    query = parse_qs(parsed.query)
    return bool(query.get("event") and query.get("option"))
