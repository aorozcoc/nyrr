"""Data contracts (spec §7).

The three spec models are kept with their exact required fields. A few optional
fields were added (documented in DECISION_LOG.md) because the eligibility engine
needs evidence the spec fields alone cannot carry (tags, restriction text, multi-day
end dates).
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Classification(StrEnum):
    AVAILABLE_VERIFIED = "AVAILABLE_VERIFIED"
    UNAVAILABLE = "UNAVAILABLE"
    UNVERIFIED = "UNVERIFIED"


class ReasonCode(StrEnum):
    # spec-defined
    WRONG_YEAR = "WRONG_YEAR"
    MEDICAL_ROLE = "MEDICAL_ROLE"
    NO_PLUS_ONE = "NO_PLUS_ONE"
    PLUS_ONE_UNCONFIRMED = "PLUS_ONE_UNCONFIRMED"
    SOLD_OUT = "SOLD_OUT"
    REGISTRATION_CLOSED = "REGISTRATION_CLOSED"
    REGISTRATION_UNCONFIRMED = "REGISTRATION_UNCONFIRMED"
    RESTRICTED_ROLE = "RESTRICTED_ROLE"
    MISSING_REGISTRATION_URL = "MISSING_REGISTRATION_URL"
    SOURCE_ERROR = "SOURCE_ERROR"
    PARSER_ERROR = "PARSER_ERROR"
    ELIGIBLE = "ELIGIBLE"
    # extensions (DECISION_LOG D-07)
    MEDICAL_UNCONFIRMED = "MEDICAL_UNCONFIRMED"
    EVENT_PASSED = "EVENT_PASSED"
    SHIFT_MISSING = "SHIFT_MISSING"
    STALE_EVIDENCE = "STALE_EVIDENCE"
    SHIFT_DATE_UNCONFIRMED = "SHIFT_DATE_UNCONFIRMED"


class DeliveryStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    ACCEPTED = "ACCEPTED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    SUPPRESSED = "SUPPRESSED"  # extension: stale availability, never sent


class VolunteerEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    event_name: str
    event_date: date
    location: str | None
    event_url: str
    # extensions
    event_end_date: date | None = None
    event_status: str | None = None
    tags: tuple[str, ...] = ()
    description: str = ""


class VolunteerShift(BaseModel):
    model_config = ConfigDict(frozen=True)

    shift_id: str
    event_id: str
    role_name: str
    start_time: datetime | None
    end_time: datetime | None
    is_medical: bool | None
    plus_one_2026: bool | None
    availability_status: str
    registration_url: str | None
    source_url: str
    verified_at: datetime
    # extensions
    tags: tuple[str, ...] = ()
    is_restricted: bool | None = None
    description: str = ""


class EligibilityResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    shift_id: str
    classification: str
    eligible: bool
    reason_codes: list[str] = Field(default_factory=list)
    evaluated_at: datetime


class CalendarEntry(BaseModel):
    """One event card from the volunteer calendar (index only — never alert evidence)."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    event_name: str
    event_url: str
    start: datetime | None
    end: datetime | None
    status: str | None
    tags: tuple[str, ...] = ()
    location: str | None = None
    description: str = ""

    @property
    def year(self) -> int | None:
        head = self.event_id.split("/", 1)[0]
        return int(head) if head.isdigit() else None
