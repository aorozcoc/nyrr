"""One monitoring cycle (spec §12).

START → validate config → 2026 cutoff → acquire DB lock → fetch NYRR → parse shifts → evaluate →
compare/persist + outbox (one transaction per event page) → dispatch emails (no open transaction) →
health → release lock → END
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .config import Settings
from .eligibility import AVAILABLE_SLUGS, evaluate_shift
from .health import floor_slot, ping_heartbeat, process_health
from .models import Classification, ReasonCode, VolunteerEvent
from .notifier import ResendSender, dispatch_pending
from .nyrr_client import AccessDenied, NyrrClient, RateLimited, RequestBudgetExceeded, SourceError
from .parser import ParserError, parse_calendar, parse_event_page
from .repository import Repository, connect

log = logging.getLogger(__name__)

LOCK_NAME = "nyrr-monitor"


@dataclass
class RunSummary:
    run_id: uuid.UUID
    status: str
    counters: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    expired: bool = False
    health_status: str | None = None


def is_expired(settings: Settings, now: datetime) -> bool:
    return now.astimezone(settings.tz).date() > settings.monitoring_end_date


def _is_medical_only(tags: tuple[str, ...]) -> bool:
    return any(t.lower() == "medical" for t in tags)


def run_cycle(
    settings: Settings,
    *,
    connect_fn: Callable[[str], object] = connect,
    client_factory: Callable[[Settings], NyrrClient] | None = None,
    sender_factory: Callable[[Settings], ResendSender | None] | None = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    heartbeat: Callable[..., bool] = ping_heartbeat,
) -> RunSummary:
    run_id = uuid.uuid4()
    now = now_fn()
    tz = settings.tz
    client_factory = client_factory or _default_client
    sender_factory = sender_factory or _default_sender

    conn = connect_fn(settings.database_url.get_secret_value())
    repo = Repository(conn)
    holder = str(run_id)
    try:
        # ---------------------------------------------------------------- end of monitoring
        if is_expired(settings, now):
            return _handle_expiry(repo, settings, run_id, now, sender_factory, sleep)

        # ---------------------------------------------------------------- lock
        if not repo.acquire_lock(LOCK_NAME, holder, now=now, ttl=timedelta(seconds=settings.lock_ttl_seconds)):
            repo.record_skipped_run(run_id, now=now, status="SKIPPED_LOCKED", trigger=settings.run_trigger,
                                    scheduled_for=floor_slot(now), note="another monitoring cycle holds the lock")
            log.warning("run_skipped_locked", extra={"run_id": str(run_id)})
            return RunSummary(run_id, "SKIPPED_LOCKED")
        try:
            return _locked_cycle(repo, settings, run_id, now, client_factory, sender_factory, now_fn, sleep, heartbeat)
        finally:
            repo.release_lock(LOCK_NAME, holder)
    finally:
        conn.close()


def _locked_cycle(repo, settings, run_id, started, client_factory, sender_factory, now_fn, sleep, heartbeat):
    tz = settings.tz
    if not settings.nyrr_collection_authorized:
        repo.record_skipped_run(run_id, now=started, status="NOT_AUTHORIZED", trigger=settings.run_trigger,
                                scheduled_for=floor_slot(started),
                                note="NYRR_COLLECTION_AUTHORIZED is not true; no NYRR requests made (Gate 1)")
        log.warning("collection_not_authorized", extra={"run_id": str(run_id)})
        return RunSummary(run_id, "NOT_AUTHORIZED")

    block = repo.get_state("collection_blocked")
    if isinstance(block, dict) and block.get("blocked"):
        # NYRR refused access earlier. Never retry on our own; an operator must clear the block.
        repo.record_skipped_run(
            run_id, now=started, status="NOT_AUTHORIZED", trigger=settings.run_trigger,
            scheduled_for=floor_slot(started),
            note=f"ACCESS_DENIED by NYRR at {block.get('at')}; collection paused. Clear with "
                 "`python scripts/check_health.py --clear-access-block` after resolving it with NYRR.")
        log.error("collection_blocked_after_access_denied", extra={"since": block.get("at")})
        return RunSummary(run_id, "NOT_AUTHORIZED", errors=["ACCESS_DENIED (persisted block)"])

    if settings.alerts_enabled and repo.get_state("production_activated_at") is None:
        repo.set_state("production_activated_at", started.isoformat(), now=started)
        log.info("production_activated", extra={"at": started.isoformat()})

    repo.start_run(run_id, started_at=started, trigger=settings.run_trigger, scheduled_for=floor_slot(started))
    c = dict(events_checked=0, shifts_checked=0, verified_openings=0, unverified_shifts=0, error_count=0,
             requests_made=0, source_failures=0, parser_errors=0, transitions_created=0,
             notifications_created=0, full_sweep=False)
    errors: list[str] = []
    access_denied = False
    backoff = False  # NYRR asked us to slow down: stop this run
    calendar_ok = False
    today = started.astimezone(tz).date()
    year = settings.monitoring_year
    sender = None

    client = client_factory(settings)
    try:
        # ---------------------------------------------------------------- calendar (index only)
        entries = []
        try:
            html, _, _ = client.get_html(settings.nyrr_calendar_path)
            entries = parse_calendar(html, settings.nyrr_base_url)
            calendar_ok = True
        except AccessDenied as exc:
            access_denied = True
            errors.append(f"ACCESS_DENIED calendar: {exc}")
        except RateLimited as exc:
            backoff = True
            c["source_failures"] += 1
            errors.append(f"RATE_LIMITED calendar: {exc}")
        except SourceError as exc:
            c["source_failures"] += 1
            errors.append(f"SOURCE_ERROR calendar: {exc}")
        except ParserError as exc:
            c["parser_errors"] += 1
            errors.append(f"PARSER_ERROR calendar: {exc}")

        entry_by_id = {e.event_id: e for e in entries if e.year == year}
        with repo.tx():
            for e in entry_by_id.values():
                if e.start is None:
                    continue
                last_day = (e.end or e.start).astimezone(tz).date()
                repo.upsert_event(VolunteerEvent(
                    event_id=e.event_id, event_name=e.event_name, event_date=e.start.astimezone(tz).date(),
                    location=e.location, event_url=e.event_url,
                    event_end_date=last_day if e.end else None, event_status=e.status, tags=e.tags,
                    description=e.description), now=started)

        # ---------------------------------------------------------------- choose event pages
        tracked = repo.tracked_event_ids(today=today)
        last_sweep_raw = repo.get_state("last_full_sweep_at")
        last_sweep = datetime.fromisoformat(last_sweep_raw) if last_sweep_raw else None
        full_sweep = calendar_ok and (
            last_sweep is None or started - last_sweep >= timedelta(minutes=settings.full_sweep_interval_minutes))
        c["full_sweep"] = full_sweep

        upcoming = {
            eid: e for eid, e in entry_by_id.items()
            if e.start is not None and (e.end or e.start).astimezone(tz).date() >= today
            and not _is_medical_only(e.tags)
        }
        priority = [eid for eid in sorted(tracked)]
        priority += [eid for eid, e in sorted(upcoming.items()) if e.status in AVAILABLE_SLUGS and eid not in priority]
        if full_sweep:
            priority += [eid for eid in sorted(upcoming) if eid not in priority]
        urls = {eid: e.event_url for eid, e in upcoming.items()}
        urls.update({k: v for k, v in repo.event_urls(set(priority) - set(urls)).items()})

        swept_all = True
        for event_id in priority:
            if access_denied or backoff:
                swept_all = False
                break
            url = urls.get(event_id)
            if url is None:
                continue
            try:
                html, final_url, fetched_at = client.get_html(url)
            except RequestBudgetExceeded as exc:
                errors.append(f"BUDGET {exc}")
                swept_all = False
                break
            except AccessDenied as exc:
                access_denied = True
                errors.append(f"ACCESS_DENIED {event_id}: {exc}")
                with repo.tx():
                    c["transitions_created"] += repo.downgrade_shifts(
                        event_id, keep_ids=None, only_available=True, reason=ReasonCode.SOURCE_ERROR,
                        run_id=run_id, now=now_fn())
                break
            except RateLimited as exc:
                backoff = True
                c["source_failures"] += 1
                errors.append(f"RATE_LIMITED {event_id}: {exc}")
                with repo.tx():
                    c["transitions_created"] += repo.downgrade_shifts(
                        event_id, keep_ids=None, only_available=True, reason=ReasonCode.SOURCE_ERROR,
                        run_id=run_id, now=now_fn())
                break
            except SourceError as exc:
                c["source_failures"] += 1
                errors.append(f"SOURCE_ERROR {event_id}: {exc}")
                with repo.tx():
                    c["transitions_created"] += repo.downgrade_shifts(
                        event_id, keep_ids=None, only_available=True, reason=ReasonCode.SOURCE_ERROR,
                        run_id=run_id, now=now_fn())
                continue
            try:
                event, shifts = parse_event_page(
                    html, url, fetched_at, tz=tz, monitoring_year=year,
                    registration_hosts=settings.registration_hosts, calendar_entry=entry_by_id.get(event_id))
            except ParserError as exc:
                c["parser_errors"] += 1
                errors.append(f"PARSER_ERROR {event_id}: {exc}")
                with repo.tx():
                    c["transitions_created"] += repo.downgrade_shifts(
                        event_id, keep_ids=None, only_available=True, reason=ReasonCode.PARSER_ERROR,
                        run_id=run_id, now=now_fn())
                continue

            c["events_checked"] += 1
            evaluated_at = now_fn()
            with repo.tx():  # atomic per event page: observations + transitions + outbox rows
                repo.upsert_event(event, now=evaluated_at)
                for shift in shifts:
                    result = evaluate_shift(
                        event, shift, now=evaluated_at, tz=tz, monitoring_year=year,
                        registration_hosts=settings.registration_hosts,
                        evidence_max_age=timedelta(minutes=settings.evidence_max_age_minutes),
                        run_started_at=started)
                    outcome = repo.apply_observation(
                        shift, result, run_id=run_id, now=evaluated_at,
                        recipients=settings.recipient_keys, alerts_enabled=settings.alerts_enabled)
                    c["shifts_checked"] += 1
                    c["verified_openings"] += result.classification == Classification.AVAILABLE_VERIFIED
                    c["unverified_shifts"] += result.classification == Classification.UNVERIFIED
                    c["transitions_created"] += outcome.transition_id is not None
                    c["notifications_created"] += outcome.notifications_created
                    if result.classification == Classification.AVAILABLE_VERIFIED:
                        log.info("verified_opening", extra={
                            "event_id": event.event_id, "shift_id": shift.shift_id, "role": shift.role_name,
                            "alert": outcome.alert_generated, "alerts_enabled": settings.alerts_enabled})
                c["transitions_created"] += repo.downgrade_shifts(
                    event.event_id, keep_ids={s.shift_id for s in shifts}, only_available=False,
                    reason=ReasonCode.SHIFT_MISSING, run_id=run_id, now=evaluated_at)

        if full_sweep and swept_all and not access_denied:
            repo.set_state("last_full_sweep_at", started.isoformat(), now=now_fn())
        if access_denied:
            repo.set_state("collection_blocked", {"blocked": True, "at": started.isoformat(),
                                                  "detail": "; ".join(e for e in errors if "ACCESS_DENIED" in e)},
                           now=now_fn())
        c["requests_made"] = client.requests_made
    finally:
        client.close()

    # -------------------------------------------------------------------- dispatch (outside transactions)
    sender = sender_factory(settings)
    try:
        if sender is not None:
            stats = dispatch_pending(
                repo, sender, tz=tz, retry_delays=settings.retry_delays_seconds,
                max_attempts=settings.max_send_attempts,
                evidence_max_age=timedelta(minutes=settings.evidence_max_age_minutes),
                wait_budget_seconds=settings.dispatch_wait_budget_seconds, now_fn=now_fn, sleep=sleep,
                resolve_recipient=settings.resolve_recipient)
            if stats.permanent_failures:
                errors.append(f"EMAIL {stats.permanent_failures} permanent failure(s)")

        c["error_count"] = c["source_failures"] + c["parser_errors"] + int(access_denied)
        if access_denied or (not calendar_ok and c["events_checked"] == 0):
            status = "FAILED"
        elif c["error_count"] or errors:
            status = "PARTIAL"
        else:
            status = "SUCCESS"
        repo.finish_run(run_id, completed_at=now_fn(), status=status, counters=c,
                        error_summary="; ".join(errors)[:2000] or None)
        log.info("run_completed", extra={"run_id": str(run_id), "status": status, **c})

        report = process_health(repo, sender, settings.alert_email_to, now=now_fn(),
                                end_date=settings.monitoring_end_date, tz=tz)
    finally:
        if sender is not None:
            sender.close()
    hb_url = settings.heartbeat_url.get_secret_value() if settings.heartbeat_url else None
    heartbeat(hb_url, ok=status in ("SUCCESS", "PARTIAL"))
    return RunSummary(run_id, status, c, errors, health_status=report.worst)


def _handle_expiry(repo: Repository, settings: Settings, run_id, now, sender_factory,
                   sleep: Callable[[float], None] = time.sleep) -> RunSummary:
    """After 2026-12-31 ET: no NYRR requests, no opening alerts, keep history, summarise once."""
    first_time = repo.get_state("expired_at") is None
    if first_time:
        summary = repo.summary_counts()
        repo.set_state("expired_at", now.isoformat(), now=now)
        repo.set_state("final_summary", summary, now=now)
        repo.record_skipped_run(run_id, now=now, status="EXPIRED", trigger=settings.run_trigger,
                                scheduled_for=floor_slot(now), note="monitoring window ended 2026-12-31 ET")
        log.info("monitoring_expired", extra={"summary": summary})
        sender = sender_factory(settings)
        if sender is not None:
            text = "\n".join([
                "The NYRR 9+1 volunteer monitor has reached its end date (December 31, 2026, Eastern Time).",
                "No further NYRR checks or opening alerts will be made. History is preserved.",
                "", "Final summary:", *[f"  {k}: {v}" for k, v in summary.items()],
            ])
            try:
                # this is the last run (the schedule is disabled right after), so retry in-process
                for r in settings.alert_email_to:
                    for attempt in range(3):
                        result = sender.send(to=r, subject="NYRR MONITOR — MONITORING PERIOD ENDED", text=text,
                                             html_body=None, idempotency_key=f"nyrr-final-summary-{r}")
                        if result.outcome.value != "retryable":
                            break
                        if attempt < 2:
                            sleep(60)
            finally:
                sender.close()
    return RunSummary(run_id, "EXPIRED", expired=True)


def _default_client(settings: Settings) -> NyrrClient:
    return NyrrClient(base_url=settings.nyrr_base_url, user_agent=settings.user_agent,
                      authorized=settings.nyrr_collection_authorized,
                      request_delay=settings.request_delay_seconds, max_requests=settings.max_requests_per_run)


def _default_sender(settings: Settings):
    if settings.email_provider == "gmail":
        if settings.gmail_app_password is None or not settings.alert_email_from:
            return None
        from .config import _bare_address
        from .notifier import GmailSender
        return GmailSender(_bare_address(settings.alert_email_from), settings.gmail_app_password.get_secret_value())
    if settings.resend_api_key is None or not settings.alert_email_from:
        return None
    return ResendSender(settings.resend_api_key.get_secret_value(), settings.alert_email_from)
