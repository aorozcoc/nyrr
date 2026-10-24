"""Deterministic shift eligibility (spec §8).

Every requirement is evaluated and every failing reason recorded. The classification is:
  UNAVAILABLE         if any requirement is *confirmed* not met (e.g. sold out, medical, No +1)
  UNVERIFIED          otherwise, if any requirement cannot be established (unknown/missing evidence)
  AVAILABLE_VERIFIED  only when every requirement is positively satisfied.
Unknown values never default to eligible.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import Classification, EligibilityResult, ReasonCode, VolunteerEvent, VolunteerShift
from .parser import registration_url_is_valid

AVAILABLE_SLUGS = frozenset({"available", "limited-availability"})
SOLD_OUT_SLUGS = frozenset({"sold-out", "full", "waitlist"})
CLOSED_SLUGS = frozenset({"closed", "registration-closed", "coming-soon", "cancelled", "canceled"})

NEGATIVE = {
    ReasonCode.WRONG_YEAR, ReasonCode.EVENT_PASSED, ReasonCode.MEDICAL_ROLE, ReasonCode.NO_PLUS_ONE,
    ReasonCode.RESTRICTED_ROLE, ReasonCode.SOLD_OUT, ReasonCode.REGISTRATION_CLOSED,
}


def evaluate_shift(
    event: VolunteerEvent,
    shift: VolunteerShift,
    *,
    now: datetime,
    tz: ZoneInfo,
    monitoring_year: int = 2026,
    registration_hosts: tuple[str, ...] = ("register.nyrr.org",),
    evidence_max_age: timedelta = timedelta(minutes=20),
    run_started_at: datetime | None = None,
) -> EligibilityResult:
    reasons: list[ReasonCode] = []
    today = now.astimezone(tz).date()

    # 1. 2026 window -------------------------------------------------------------------
    year_in_url = event.event_id.split("/", 1)[0]
    if event.event_date.year != monitoring_year or year_in_url != str(monitoring_year):
        reasons.append(ReasonCode.WRONG_YEAR)
    last_day = event.event_end_date or event.event_date
    if last_day < today:
        reasons.append(ReasonCode.EVENT_PASSED)
    # The shift's own date must also fall in the monitoring year. Multi-day events (e.g. a
    # June 2026 – March 2027 program) can contain 2027 shifts while the event itself is "2026".
    if shift.start_time is not None:
        shift_day = shift.start_time.astimezone(tz).date()
        if shift_day.year != monitoring_year:
            reasons.append(ReasonCode.WRONG_YEAR)
        elif shift_day < today:
            reasons.append(ReasonCode.EVENT_PASSED)
    elif last_day.year != monitoring_year:
        reasons.append(ReasonCode.SHIFT_DATE_UNCONFIRMED)
    if shift.event_id != event.event_id:
        reasons.append(ReasonCode.PARSER_ERROR)

    # 2. non-medical ---------------------------------------------------------------------
    if shift.is_medical is True:
        reasons.append(ReasonCode.MEDICAL_ROLE)
    elif shift.is_medical is None:
        reasons.append(ReasonCode.MEDICAL_UNCONFIRMED)

    # 3. explicit 2026 +1 ----------------------------------------------------------------
    if shift.plus_one_2026 is False:
        reasons.append(ReasonCode.NO_PLUS_ONE)
    elif shift.plus_one_2026 is None:
        reasons.append(ReasonCode.PLUS_ONE_UNCONFIRMED)

    # 4. not restricted ------------------------------------------------------------------
    if shift.is_restricted is True:
        reasons.append(ReasonCode.RESTRICTED_ROLE)
    elif shift.is_restricted is None:
        reasons.append(ReasonCode.REGISTRATION_UNCONFIRMED)

    # 5. individual-shift availability ---------------------------------------------------
    status = (shift.availability_status or "").strip().lower()
    if status in SOLD_OUT_SLUGS:
        reasons.append(ReasonCode.SOLD_OUT)
    elif status in CLOSED_SLUGS:
        reasons.append(ReasonCode.REGISTRATION_CLOSED)
    elif status not in AVAILABLE_SLUGS:
        reasons.append(ReasonCode.REGISTRATION_UNCONFIRMED)

    # 6. registration URL ----------------------------------------------------------------
    if not registration_url_is_valid(shift.registration_url, registration_hosts):
        if status in AVAILABLE_SLUGS:
            reasons.append(ReasonCode.MISSING_REGISTRATION_URL)
        # for sold-out/closed shifts a missing link is expected and adds no information

    # 7. fresh, shift-level evidence ------------------------------------------------------
    verified_at = shift.verified_at
    if verified_at.tzinfo is None or now - verified_at > evidence_max_age or verified_at > now + timedelta(minutes=1):
        reasons.append(ReasonCode.STALE_EVIDENCE)
    elif run_started_at is not None and verified_at < run_started_at:
        reasons.append(ReasonCode.STALE_EVIDENCE)

    if not reasons:
        classification = Classification.AVAILABLE_VERIFIED
        reasons = [ReasonCode.ELIGIBLE]
    elif any(r in NEGATIVE for r in reasons):
        classification = Classification.UNAVAILABLE
    else:
        classification = Classification.UNVERIFIED

    return EligibilityResult(
        shift_id=shift.shift_id,
        classification=classification.value,
        eligible=classification is Classification.AVAILABLE_VERIFIED,
        reason_codes=[r.value for r in dict.fromkeys(reasons)],
        evaluated_at=now,
    )
