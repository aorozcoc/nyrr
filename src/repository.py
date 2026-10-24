"""PostgreSQL persistence (psycopg 3). All SQL is parameterised.

Connection notes
- Table names are schema-qualified, so the code works through Supabase's Supavisor pooler
  in either session or transaction mode without relying on `search_path`.
- Server-side prepared statements are disabled (`prepare_threshold=None`) for pooler safety.
- Non-local connections default to `sslmode=require`.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row

from .models import Classification, DeliveryStatus, EligibilityResult, ReasonCode, VolunteerEvent, VolunteerShift

S = "nyrr_monitor."  # schema prefix (constant, never user input)
AVAILABLE = Classification.AVAILABLE_VERIFIED.value
UNAVAILABLE = Classification.UNAVAILABLE.value
UNVERIFIED = Classification.UNVERIFIED.value


def connect(database_url: str, *, application_name: str = "nyrr-monitor") -> psycopg.Connection:
    kwargs: dict[str, Any] = {
        "autocommit": True,
        "connect_timeout": 10,
        "application_name": application_name,
        "prepare_threshold": None,
        "row_factory": dict_row,
    }
    host = urlparse(database_url).hostname or ""
    if "sslmode=" not in database_url and host not in ("", "localhost", "127.0.0.1", "::1"):
        kwargs["sslmode"] = "require"
    return psycopg.connect(database_url, **kwargs)


def notification_idempotency_key(transition_id: uuid.UUID, recipient: str) -> str:
    digest = hashlib.sha256(recipient.lower().encode()).hexdigest()[:16]
    return f"nyrr-opening-{transition_id}-{digest}"


@dataclass(frozen=True)
class ObservationOutcome:
    transition_id: uuid.UUID | None
    previous: str | None
    new: str
    alert_generated: bool
    notifications_created: int


@dataclass(frozen=True)
class NotificationJob:
    notification_id: uuid.UUID
    transition_id: uuid.UUID
    recipient_email: str
    attempt_count: int
    idempotency_key: str
    shift_id: str
    # live state, used only for the freshness check at send time
    shift_classification: str
    shift_is_latest_transition: bool
    shift_verified_at: datetime | None
    # frozen alert content (from availability_transitions.alert_snapshot)
    role_name: str
    start_time: datetime | None
    end_time: datetime | None
    registration_url: str | None
    plus_one_2026: bool | None
    availability_status: str
    event_name: str
    event_date: Any
    event_end_date: Any
    location: str | None
    event_url: str
    verified_at: datetime | None
    detected_at: datetime


def _job_from_row(row: dict) -> NotificationJob:
    snap = row.pop("alert_snapshot") or {}

    def dt(key):
        return datetime.fromisoformat(snap[key]) if snap.get(key) else None

    def d(key):
        return date.fromisoformat(snap[key]) if snap.get(key) else None

    return NotificationJob(
        **row, role_name=snap.get("role_name", ""), start_time=dt("start_time"), end_time=dt("end_time"),
        registration_url=snap.get("registration_url"), plus_one_2026=snap.get("plus_one_2026"),
        availability_status=snap.get("availability_status", ""), event_name=snap.get("event_name", ""),
        event_date=d("event_date"), event_end_date=d("event_end_date"), location=snap.get("location"),
        event_url=snap.get("event_url", ""), verified_at=dt("verified_at"),
    )


class Repository:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    @contextmanager
    def tx(self):
        with self.conn.transaction():
            yield

    def _one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        return self.conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        return self.conn.execute(sql, params).fetchall()

    # ================================================================= locks
    def acquire_lock(self, name: str, holder: str, *, now: datetime, ttl: timedelta) -> bool:
        row = self._one(
            f"""INSERT INTO {S}locks (lock_name, holder, acquired_at, expires_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (lock_name) DO UPDATE
                   SET holder = EXCLUDED.holder, acquired_at = EXCLUDED.acquired_at,
                       expires_at = EXCLUDED.expires_at
                 WHERE {S}locks.expires_at < EXCLUDED.acquired_at
                RETURNING holder""",
            (name, holder, now, now + ttl),
        )
        return row is not None and row["holder"] == holder

    def release_lock(self, name: str, holder: str) -> None:
        self.conn.execute(f"DELETE FROM {S}locks WHERE lock_name = %s AND holder = %s", (name, holder))

    # ================================================================= app state
    def get_state(self, key: str) -> Any:
        row = self._one(f"SELECT value FROM {S}app_state WHERE key = %s", (key,))
        return None if row is None else row["value"]

    def set_state(self, key: str, value: Any, *, now: datetime) -> None:
        self.conn.execute(
            f"""INSERT INTO {S}app_state (key, value, updated_at) VALUES (%s, %s::jsonb, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at""",
            (key, json.dumps(value, default=str), now),
        )

    # ================================================================= runs
    def start_run(self, run_id: uuid.UUID, *, started_at: datetime, trigger: str, scheduled_for: datetime | None) -> None:
        self.conn.execute(
            f"""INSERT INTO {S}monitoring_runs (run_id, started_at, status, trigger, scheduled_for)
                VALUES (%s, %s, 'RUNNING', %s, %s)""",
            (run_id, started_at, trigger, scheduled_for),
        )

    def finish_run(self, run_id: uuid.UUID, *, completed_at: datetime, status: str, counters: dict[str, int],
                   error_summary: str | None) -> None:
        allowed = {"events_checked", "shifts_checked", "verified_openings", "unverified_shifts", "error_count",
                   "requests_made", "source_failures", "parser_errors", "transitions_created",
                   "notifications_created", "full_sweep"}
        sets = ", ".join(f"{k} = %({k})s" for k in counters if k in allowed)
        params = {k: v for k, v in counters.items() if k in allowed}
        params.update(run_id=run_id, completed_at=completed_at, status=status, error_summary=error_summary)
        self.conn.execute(
            f"""UPDATE {S}monitoring_runs SET completed_at = %(completed_at)s, status = %(status)s,
                error_summary = %(error_summary)s{', ' + sets if sets else ''} WHERE run_id = %(run_id)s""",
            params,
        )

    def record_skipped_run(self, run_id: uuid.UUID, *, now: datetime, status: str, trigger: str,
                           scheduled_for: datetime | None, note: str) -> None:
        self.conn.execute(
            f"""INSERT INTO {S}monitoring_runs (run_id, started_at, completed_at, status, trigger,
                                                 scheduled_for, error_summary)
                VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (run_id, now, now, status, trigger, scheduled_for, note),
        )

    def recent_runs(self, limit: int = 20) -> list[dict]:
        return self._all(f"SELECT * FROM {S}monitoring_runs ORDER BY started_at DESC LIMIT %s", (limit,))

    def last_successful_run(self) -> dict | None:
        return self._one(
            f"""SELECT * FROM {S}monitoring_runs WHERE status IN ('SUCCESS', 'PARTIAL')
                ORDER BY completed_at DESC NULLS LAST LIMIT 1"""
        )

    def runs_since(self, since: datetime) -> list[dict]:
        return self._all(
            f"SELECT started_at, scheduled_for, status FROM {S}monitoring_runs WHERE started_at >= %s ORDER BY started_at",
            (since,),
        )

    # ================================================================= events & shifts
    def upsert_event(self, event: VolunteerEvent, *, now: datetime) -> None:
        self.conn.execute(
            f"""INSERT INTO {S}events (event_id, event_name, event_date, location, event_url, event_end_date,
                                       event_status, event_tags, last_seen_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO UPDATE SET
                    event_name = EXCLUDED.event_name, event_date = EXCLUDED.event_date,
                    location = COALESCE(EXCLUDED.location, {S}events.location),
                    event_url = EXCLUDED.event_url, event_end_date = EXCLUDED.event_end_date,
                    event_status = COALESCE(EXCLUDED.event_status, {S}events.event_status),
                    event_tags = EXCLUDED.event_tags, last_seen_at = EXCLUDED.last_seen_at,
                    updated_at = EXCLUDED.updated_at""",
            (event.event_id, event.event_name, event.event_date, event.location, event.event_url,
             event.event_end_date, event.event_status, list(event.tags), now, now, now),
        )

    def tracked_event_ids(self, *, today) -> set[str]:
        """Upcoming events with shifts currently open or unresolved — always re-checked.

        Events whose last day has passed are excluded: their pages may disappear (404) and
        re-fetching them forever would only produce source failures.
        """
        rows = self._all(
            f"""SELECT DISTINCT s.event_id FROM {S}shifts s JOIN {S}events e ON e.event_id = s.event_id
                WHERE s.classification IN (%s, %s) AND COALESCE(e.event_end_date, e.event_date) >= %s""",
            (AVAILABLE, UNVERIFIED, today),
        )
        return {r["event_id"] for r in rows}

    def event_urls(self, event_ids: set[str]) -> dict[str, str]:
        if not event_ids:
            return {}
        rows = self._all(f"SELECT event_id, event_url FROM {S}events WHERE event_id = ANY(%s)", (list(event_ids),))
        return {r["event_id"]: r["event_url"] for r in rows}

    def get_shift(self, shift_id: str) -> dict | None:
        return self._one(f"SELECT * FROM {S}shifts WHERE shift_id = %s", (shift_id,))

    def shift_ids_for_event(self, event_id: str) -> set[str]:
        return {r["shift_id"] for r in self._all(f"SELECT shift_id FROM {S}shifts WHERE event_id = %s", (event_id,))}

    def apply_observation(
        self,
        shift: VolunteerShift,
        result: EligibilityResult,
        *,
        run_id: uuid.UUID,
        now: datetime,
        recipients: tuple[str, ...],
        alerts_enabled: bool,
    ) -> ObservationOutcome:
        """Persist one fresh observation. Must be called inside `tx()`.

        Atomic unit (spec §9): classification update + transition + both notification rows.
        The row lock (FOR UPDATE) plus UNIQUE(shift_id, from_version) make concurrent runs safe.
        """
        new = result.classification
        evidence = (
            shift.role_name, shift.start_time, shift.end_time, shift.is_medical, shift.plus_one_2026,
            shift.availability_status, shift.registration_url, shift.source_url, shift.verified_at,
            list(shift.tags), shift.is_restricted, result.reason_codes, run_id, now,
        )
        # Insert-if-absent first so concurrent first sightings serialise on the row instead of colliding.
        created = self._one(
            f"""INSERT INTO {S}shifts (shift_id, event_id, classification, state_version, alert_armed,
                    role_name, start_time, end_time, is_medical, plus_one_2026, availability_status,
                    registration_url, source_url, verified_at, tags, is_restricted, reason_codes,
                    last_observed_run_id, updated_at)
                VALUES (%s, %s, %s, 0, TRUE, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s)
                ON CONFLICT (shift_id) DO NOTHING RETURNING shift_id""",
            (shift.shift_id, shift.event_id, UNVERIFIED, *evidence[:8], *evidence[9:]),
        )
        current = self._one(
            f"""SELECT classification, state_version, alert_armed, verified_at FROM {S}shifts
                WHERE shift_id = %s FOR UPDATE""",
            (shift.shift_id,),
        )
        if created is not None:
            previous, version, armed = None, 0, True
        else:
            previous, version, armed = current["classification"], current["state_version"], current["alert_armed"]
            if current["verified_at"] is not None and shift.verified_at < current["verified_at"]:
                # an older observation arriving after a newer one must never overwrite it
                return ObservationOutcome(None, previous, previous, False, 0)

        wants_alert = new == AVAILABLE and armed and alerts_enabled
        changed = previous != new
        transition_id: uuid.UUID | None = None
        n_created = 0

        if changed or wants_alert:
            transition_id = uuid.uuid4()
            reasons = list(result.reason_codes)
            if not changed:
                reasons.append("FIRST_ALERT_AFTER_ACTIVATION")
            snapshot = self._alert_snapshot(shift) if wants_alert else None
            inserted = self._one(
                f"""INSERT INTO {S}availability_transitions (transition_id, shift_id, previous_classification,
                        new_classification, detected_at, run_id, from_version, reason_codes, alert_generated,
                        alert_snapshot)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (shift_id, from_version) DO NOTHING
                    RETURNING transition_id""",
                (transition_id, shift.shift_id, previous, new, now, run_id, version, reasons, wants_alert,
                 json.dumps(snapshot) if snapshot else None),
            )
            if inserted is None:  # another run already recorded this exact change
                return ObservationOutcome(None, previous, new, False, 0)
            version += 1
            if wants_alert:
                for recipient in recipients:
                    self.conn.execute(
                        f"""INSERT INTO {S}notifications (notification_id, transition_id, recipient_email,
                                delivery_status, attempt_count, next_attempt_at, idempotency_key, created_at)
                            VALUES (%s, %s, %s, 'PENDING', 0, %s, %s, %s)""",
                        (uuid.uuid4(), transition_id, recipient, now,
                         notification_idempotency_key(transition_id, recipient), now),
                    )
                    n_created += 1

        new_armed = armed
        if new == UNAVAILABLE:
            new_armed = True  # confirmed closure re-arms reopening alerts
        if wants_alert:
            new_armed = False
        self.conn.execute(
            f"""UPDATE {S}shifts SET classification = %s, state_version = %s, alert_armed = %s,
                    role_name = %s, start_time = %s, end_time = %s, is_medical = %s, plus_one_2026 = %s,
                    availability_status = %s, registration_url = %s, source_url = %s, verified_at = %s,
                    tags = %s, is_restricted = %s, reason_codes = %s, last_observed_run_id = %s, updated_at = %s,
                    last_alerted_at = CASE WHEN %s THEN %s ELSE last_alerted_at END
                WHERE shift_id = %s""",
            (new, version, new_armed, *evidence, wants_alert, now, shift.shift_id),
        )
        return ObservationOutcome(transition_id if (changed or wants_alert) else None, previous, new, wants_alert, n_created)

    def _alert_snapshot(self, shift: VolunteerShift) -> dict:
        """Freeze the alert's content at detection time. Retries re-send exactly this, so the
        payload under one Resend Idempotency-Key never changes (Resend rejects changed payloads)."""
        ev = self._one(
            f"SELECT event_name, event_date, event_end_date, location, event_url FROM {S}events WHERE event_id = %s",
            (shift.event_id,),
        ) or {}

        def iso(v):
            return v.isoformat() if v is not None else None

        return {
            "event_name": ev.get("event_name", ""), "event_date": iso(ev.get("event_date")),
            "event_end_date": iso(ev.get("event_end_date")), "location": ev.get("location"),
            "event_url": ev.get("event_url", shift.source_url), "role_name": shift.role_name,
            "start_time": iso(shift.start_time), "end_time": iso(shift.end_time),
            "registration_url": shift.registration_url, "availability_status": shift.availability_status,
            "plus_one_2026": shift.plus_one_2026, "verified_at": iso(shift.verified_at),
        }

    def rearm_if_never_delivered(self, transition_id: uuid.UUID) -> bool:
        """If every e-mail of an alert was suppressed (nobody was told), re-arm the shift so the
        next verified observation alerts. Without this a crash between detection and dispatch,
        followed by a transient error, would silence a genuine opening forever."""
        cur = self.conn.execute(
            f"""UPDATE {S}shifts s SET alert_armed = TRUE
                  FROM {S}availability_transitions t
                 WHERE t.transition_id = %s AND s.shift_id = t.shift_id AND NOT s.alert_armed
                   AND NOT EXISTS (SELECT 1 FROM {S}notifications n
                                    WHERE n.transition_id = t.transition_id
                                      AND n.delivery_status <> 'SUPPRESSED')""",
            (transition_id,),
        )
        return cur.rowcount > 0

    def downgrade_shifts(self, event_id: str, *, keep_ids: set[str] | None, only_available: bool,
                         reason: ReasonCode, run_id: uuid.UUID, now: datetime) -> int:
        """Mark shifts UNVERIFIED without new evidence (missing from page, or source failure).

        Never re-arms alerts: a later re-verification of a previously alerted shift therefore
        does not re-alert unless a confirmed UNAVAILABLE was observed in between.
        Must be called inside `tx()`.
        """
        rows = self._all(
            f"""SELECT shift_id, classification, state_version FROM {S}shifts
                WHERE event_id = %s AND classification <> %s FOR UPDATE""",
            (event_id, UNVERIFIED),
        )
        count = 0
        for row in rows:
            if keep_ids is not None and row["shift_id"] in keep_ids:
                continue
            if only_available and row["classification"] != AVAILABLE:
                continue
            inserted = self._one(
                f"""INSERT INTO {S}availability_transitions (transition_id, shift_id, previous_classification,
                        new_classification, detected_at, run_id, from_version, reason_codes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (shift_id, from_version) DO NOTHING RETURNING transition_id""",
                (uuid.uuid4(), row["shift_id"], row["classification"], UNVERIFIED, now, run_id,
                 row["state_version"], [reason.value]),
            )
            if inserted is None:
                continue
            self.conn.execute(
                f"""UPDATE {S}shifts SET classification = %s, state_version = state_version + 1,
                        reason_codes = %s, updated_at = %s WHERE shift_id = %s""",
                (UNVERIFIED, [reason.value], now, row["shift_id"]),
            )
            count += 1
        return count

    # ================================================================= notifications (outbox)
    def recover_stale_sending(self, *, now: datetime, older_than: timedelta = timedelta(minutes=10)) -> int:
        cur = self.conn.execute(
            f"""UPDATE {S}notifications SET delivery_status = 'FAILED_RETRYABLE', next_attempt_at = %s,
                    last_error = 'recovered from SENDING (process ended mid-send); idempotency key prevents duplicates'
                WHERE delivery_status = 'SENDING' AND last_attempt_at < %s""",
            (now, now - older_than),
        )
        return cur.rowcount

    def claim_due_notifications(self, *, now: datetime, limit: int = 20) -> list[NotificationJob]:
        with self.tx():
            claimed = self._all(
                f"""WITH due AS (
                        SELECT notification_id FROM {S}notifications
                         WHERE delivery_status IN ('PENDING', 'FAILED_RETRYABLE') AND next_attempt_at <= %s
                         ORDER BY created_at, recipient_email
                         FOR UPDATE SKIP LOCKED LIMIT %s)
                    UPDATE {S}notifications n
                       SET delivery_status = 'SENDING', attempt_count = n.attempt_count + 1, last_attempt_at = %s
                      FROM due WHERE n.notification_id = due.notification_id
                    RETURNING n.notification_id""",
                (now, limit, now),
            )
        if not claimed:
            return []
        rows = self._all(
            f"""SELECT n.notification_id, n.transition_id, n.recipient_email, n.attempt_count, n.idempotency_key,
                       s.shift_id, s.classification AS shift_classification,
                       (t.from_version = s.state_version - 1) AS shift_is_latest_transition,
                       s.verified_at AS shift_verified_at, t.alert_snapshot, t.detected_at
                  FROM {S}notifications n
                  JOIN {S}availability_transitions t ON t.transition_id = n.transition_id
                  JOIN {S}shifts s ON s.shift_id = t.shift_id
                 WHERE n.notification_id = ANY(%s)
                 ORDER BY n.created_at, n.recipient_email""",
            ([r["notification_id"] for r in claimed],),
        )
        return [_job_from_row(r) for r in rows]

    def mark_notification(self, notification_id: uuid.UUID, status: DeliveryStatus, *, now: datetime,
                          message_id: str | None = None, error: str | None = None,
                          next_attempt_at: datetime | None = None, needs_review: bool = False) -> None:
        self.conn.execute(
            f"""UPDATE {S}notifications SET delivery_status = %s,
                    provider_message_id = COALESCE(%s, provider_message_id),
                    accepted_at = CASE WHEN %s = 'ACCEPTED' THEN %s ELSE accepted_at END,
                    last_error = %s, next_attempt_at = COALESCE(%s, next_attempt_at),
                    needs_review = needs_review OR %s
                WHERE notification_id = %s AND delivery_status = 'SENDING'""",
            (status.value, message_id, status.value, now, error, next_attempt_at, needs_review, notification_id),
        )

    def next_retry_due(self) -> datetime | None:
        row = self._one(
            f"""SELECT MIN(next_attempt_at) AS due FROM {S}notifications
                WHERE delivery_status IN ('PENDING', 'FAILED_RETRYABLE')"""
        )
        return row["due"] if row else None

    def notification_counts(self) -> dict[str, int]:
        rows = self._all(f"SELECT delivery_status, COUNT(*) AS n FROM {S}notifications GROUP BY delivery_status")
        return {r["delivery_status"]: r["n"] for r in rows}

    def notifications_needing_review(self) -> list[dict]:
        return self._all(
            f"""SELECT notification_id, recipient_email, last_error, last_attempt_at FROM {S}notifications
                WHERE needs_review AND delivery_status = 'FAILED_PERMANENT' ORDER BY last_attempt_at DESC"""
        )

    # ================================================================= health incidents
    def open_incident(self, incident_type: str, severity: str, details: str, *, now: datetime) -> tuple[dict, bool]:
        """Open or refresh the incident. Returns (row, newly_opened)."""
        with self.tx():
            row = self._one(
                f"""SELECT * FROM {S}health_incidents WHERE incident_type = %s AND resolved_at IS NULL FOR UPDATE""",
                (incident_type,),
            )
            if row is not None:
                # escalation DEGRADED -> CRITICAL clears alert_sent_at so the escalation is e-mailed once
                # (all expressions below see the row's old values)
                row = self._one(
                    f"""UPDATE {S}health_incidents SET last_seen_at = %s, details = %s,
                            severity = CASE WHEN %s = 'CRITICAL' THEN 'CRITICAL' ELSE severity END,
                            alert_sent_at = CASE WHEN %s = 'CRITICAL' AND severity <> 'CRITICAL'
                                                 THEN NULL ELSE alert_sent_at END
                        WHERE incident_id = %s RETURNING *""",
                    (now, details, severity, severity, row["incident_id"]),
                )
                return row, False
            row = self._one(
                f"""INSERT INTO {S}health_incidents (incident_id, incident_type, severity, opened_at, last_seen_at, details)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (incident_type) WHERE resolved_at IS NULL DO NOTHING RETURNING *""",
                (uuid.uuid4(), incident_type, severity, now, now, details),
            )
            if row is None:  # concurrent opener won
                return self._one(
                    f"SELECT * FROM {S}health_incidents WHERE incident_type = %s AND resolved_at IS NULL",
                    (incident_type,)), False
            return row, True

    def mark_incident_alerted(self, incident_id: uuid.UUID, *, now: datetime) -> None:
        self.conn.execute(f"UPDATE {S}health_incidents SET alert_sent_at = %s WHERE incident_id = %s", (now, incident_id))

    def resolve_incidents(self, active_types: set[str], *, now: datetime, managed_types: set[str]) -> list[str]:
        rows = self._all(
            f"""UPDATE {S}health_incidents SET resolved_at = %s
                WHERE resolved_at IS NULL AND incident_type = ANY(%s) AND NOT (incident_type = ANY(%s))
                RETURNING incident_type""",
            (now, list(managed_types), list(active_types)),
        )
        return [r["incident_type"] for r in rows]

    def open_incidents(self) -> list[dict]:
        return self._all(f"SELECT * FROM {S}health_incidents WHERE resolved_at IS NULL ORDER BY opened_at")

    # ================================================================= summaries
    def summary_counts(self) -> dict[str, int]:
        row = self._one(
            f"""SELECT
                  (SELECT COUNT(*) FROM {S}monitoring_runs) AS runs,
                  (SELECT COUNT(*) FROM {S}monitoring_runs WHERE status IN ('SUCCESS','PARTIAL')) AS successful_runs,
                  (SELECT COUNT(*) FROM {S}events) AS events,
                  (SELECT COUNT(*) FROM {S}shifts) AS shifts,
                  (SELECT COUNT(*) FROM {S}availability_transitions) AS transitions,
                  (SELECT COUNT(*) FROM {S}availability_transitions WHERE alert_generated) AS alerts,
                  (SELECT COUNT(*) FROM {S}notifications WHERE delivery_status = 'ACCEPTED') AS emails_accepted"""
        )
        return dict(row or {})
