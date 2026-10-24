"""Operational health (spec §15).

A job cannot detect its own failure to start, so health is checked from two independent places:
  1. `health.yml` — a separate scheduled GitHub workflow running `scripts/check_health.py`.
  2. An optional external heartbeat (e.g. healthchecks.io) pinged by every successful monitor run;
     the external service emails the owner if pings stop. This also covers a GitHub-wide outage,
     which (1) cannot.
Incidents are de-duplicated in the database: one alert per unresolved incident.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .repository import Repository

log = logging.getLogger(__name__)

HEALTHY, DEGRADED, CRITICAL, INACTIVE = "HEALTHY", "DEGRADED", "CRITICAL", "INACTIVE"
HEALTHY_MAX = timedelta(minutes=25)
DEGRADED_MAX = timedelta(minutes=45)
SLOT = timedelta(minutes=15)

MANAGED_INCIDENTS = {
    "STALE_MONITORING", "MISSED_INTERVALS", "SOURCE_FAILURES", "ACCESS_DENIED", "PARSER_CHANGE",
    "EMAIL_PROVIDER",
}


@dataclass(frozen=True)
class Issue:
    incident_type: str
    severity: str  # DEGRADED | CRITICAL
    detail: str


@dataclass
class HealthReport:
    status: str
    checked_at: datetime
    last_success_at: datetime | None
    issues: list[Issue] = field(default_factory=list)

    @property
    def worst(self) -> str:
        if any(i.severity == CRITICAL for i in self.issues):
            return CRITICAL
        if any(i.severity == DEGRADED for i in self.issues):
            return DEGRADED
        return self.status


def floor_slot(dt: datetime) -> datetime:
    dt = dt.astimezone(UTC)
    return dt.replace(minute=dt.minute - dt.minute % 15, second=0, microsecond=0)


def staleness_status(last_success_at: datetime | None, *, now: datetime, activated_at: datetime | None,
                     end_date: date, tz: ZoneInfo) -> str:
    if activated_at is None or now < activated_at or now.astimezone(tz).date() > end_date:
        return INACTIVE
    reference = max(last_success_at, activated_at) if last_success_at else activated_at
    age = now - reference
    if age <= HEALTHY_MAX:
        return HEALTHY
    if age <= DEGRADED_MAX:
        return DEGRADED
    return CRITICAL


def consecutive_missed_slots(run_starts: list[datetime], *, now: datetime, activated_at: datetime,
                             lookback: timedelta = timedelta(hours=3)) -> int:
    """Count most-recent consecutive completed 15-minute slots with no run starting inside them."""
    start = max(floor_slot(activated_at) + SLOT, floor_slot(now - lookback))
    last_complete = floor_slot(now) - SLOT
    served = {floor_slot(s) for s in run_starts}
    missed = 0
    slot = last_complete
    while slot >= start:
        if slot in served:
            break
        missed += 1
        slot -= SLOT
    return missed


def evaluate_health(repo: Repository, *, now: datetime, end_date: date, tz: ZoneInfo) -> HealthReport:
    activated_raw = repo.get_state("production_activated_at")
    activated_at = datetime.fromisoformat(activated_raw) if activated_raw else None
    last = repo.last_successful_run()
    last_success_at = last["completed_at"] if last else None
    status = staleness_status(last_success_at, now=now, activated_at=activated_at, end_date=end_date, tz=tz)
    report = HealthReport(status=status, checked_at=now, last_success_at=last_success_at)
    if status == INACTIVE:
        return report

    if status in (DEGRADED, CRITICAL):
        age = now - (last_success_at or activated_at)
        report.issues.append(Issue("STALE_MONITORING", status,
                                   f"No successful monitoring run for {int(age.total_seconds() // 60)} minutes."))

    runs = repo.runs_since(max(activated_at, now - timedelta(hours=3)))
    missed = consecutive_missed_slots([r["started_at"] for r in runs], now=now, activated_at=activated_at)
    if missed >= 2:
        report.issues.append(Issue("MISSED_INTERVALS", DEGRADED,
                                   f"{missed} consecutive 15-minute intervals had no monitoring run "
                                   "(GitHub Actions schedule delayed or skipped)."))

    recent = [r for r in repo.recent_runs(10) if r["status"] not in ("SKIPPED_LOCKED", "RUNNING")]
    last3 = recent[:3]
    if len(last3) == 3 and all(r["source_failures"] > 0 or r["status"] == "FAILED" for r in last3):
        report.issues.append(Issue("SOURCE_FAILURES", CRITICAL,
                                   "The last 3 monitoring runs each failed to retrieve NYRR pages."))
    if recent and "ACCESS_DENIED" in (recent[0].get("error_summary") or ""):
        report.issues.append(Issue("ACCESS_DENIED", CRITICAL,
                                   "NYRR returned 401/403. Collection stopped; the monitor will not try to bypass it."))
    if recent and recent[0]["parser_errors"] > 0:
        report.issues.append(Issue("PARSER_CHANGE", CRITICAL,
                                   f"{recent[0]['parser_errors']} NYRR page(s) did not match the expected structure. "
                                   "No alerts are generated from unparseable pages."))
    review = repo.notifications_needing_review()
    if review:
        report.issues.append(Issue("EMAIL_PROVIDER", CRITICAL,
                                   f"{len(review)} alert email(s) failed permanently after retries "
                                   f"(latest error: {review[0]['last_error']})."))
    return report


def render_health_email(report: HealthReport, issues: list[Issue], tz: ZoneInfo) -> tuple[str, str]:
    def fmt(dt: datetime | None) -> str:
        return dt.astimezone(tz).strftime("%Y-%m-%d %I:%M %p %Z") if dt else "never"

    lines = [
        "This is an OPERATIONAL alert about the monitor itself. It is NOT a volunteer opening.",
        "",
        f"Overall status: {report.worst}",
        f"Checked at:     {fmt(report.checked_at)}",
        f"Last successful monitoring run: {fmt(report.last_success_at)}",
        "",
        "Issues:",
        *[f"  - [{i.severity}] {i.incident_type}: {i.detail}" for i in issues],
        "",
        "While this is unresolved, new volunteer openings may be missed. See docs/TROUBLESHOOTING.md.",
        "You will not be emailed again about the same unresolved issue.",
    ]
    return "NYRR MONITOR — SYSTEM HEALTH ALERT", "\n".join(lines)


def process_health(repo: Repository, sender, recipients: tuple[str, ...], *, now: datetime, end_date: date,
                   tz: ZoneInfo) -> HealthReport:
    """Evaluate, open/resolve incidents, and email once per newly opened incident."""
    report = evaluate_health(repo, now=now, end_date=end_date, tz=tz)
    active = {i.incident_type for i in report.issues}
    resolved = repo.resolve_incidents(active, now=now, managed_types=MANAGED_INCIDENTS)
    if resolved:
        log.info("health_incidents_resolved", extra={"types": resolved})
    to_alert: list[tuple[dict, Issue]] = []
    for issue in report.issues:
        row, _new = repo.open_incident(issue.incident_type, issue.severity, issue.detail, now=now)
        if row.get("alert_sent_at") is None:
            to_alert.append((row, issue))
    if to_alert and sender is not None:
        subject, text = render_health_email(report, [i for _, i in to_alert], tz)
        key_base = "-".join(sorted(str(r["incident_id"]) for r, _ in to_alert))
        digest = hashlib.sha256(key_base.encode()).hexdigest()[:24]
        sent_any = False
        for recipient in recipients:
            rdigest = hashlib.sha256(recipient.encode()).hexdigest()[:12]
            result = sender.send(to=recipient, subject=subject, text=text, html_body=None,
                                 idempotency_key=f"nyrr-health-{digest}-{rdigest}")
            sent_any |= result.outcome.value == "accepted"
            if result.outcome.value != "accepted":
                log.error("health_alert_send_failed", extra={"error": result.error})
        if sent_any:
            for row, _ in to_alert:
                repo.mark_incident_alerted(row["incident_id"], now=now)
    log.info("health_evaluated", extra={"status": report.worst, "issues": sorted(active)})
    return report


def ping_heartbeat(url: str | None, *, ok: bool, transport: httpx.BaseTransport | None = None) -> bool:
    """Ping an external heartbeat monitor (e.g. https://hc-ping.com/<uuid>). Never raises."""
    if not url:
        return False
    target = url if ok else url.rstrip("/") + "/fail"
    try:
        with httpx.Client(timeout=10.0, transport=transport) as client:
            return client.get(target).status_code < 400
    except httpx.HTTPError:
        log.warning("heartbeat_ping_failed")
        return False


# ---------------------------------------------------------------------- lite mode (no external heartbeat)
FRESHNESS_ALERT_WINDOW = (timedelta(minutes=45), timedelta(minutes=105))


def freshness_status(last_state_at: datetime | None, *, now: datetime, end_date: date,
                     tz: ZoneInfo) -> tuple[str, bool, timedelta | None]:
    """Classify how long ago the monitor last saved its state (one save per completed run).

    Returns (status, should_alert, age). The hourly health workflow alerts only while the age is
    inside a 60-minute window that starts at the CRITICAL threshold, so one stoppage produces one
    e-mail instead of one per hour.
    """
    if last_state_at is None or now.astimezone(tz).date() > end_date:
        return INACTIVE, False, None
    age = now - last_state_at
    if age <= HEALTHY_MAX:
        status = HEALTHY
    elif age <= DEGRADED_MAX:
        status = DEGRADED
    else:
        status = CRITICAL
    lo, hi = FRESHNESS_ALERT_WINDOW
    return status, lo < age <= hi, age
