"""Phase 16 — 60-minute email delivery test (4 cycles, 15 minutes apart).

Lifecycle
- `start`  (manual workflow_dispatch, explicit confirmation): records activation time, sends cycle 1.
- `tick`   (scheduled every 15 min): sends the next cycle when due; records expected vs actual time.
- ends after cycle 4 or when 60 minutes have passed, whichever comes first; the workflow then disables
  its own schedule. A new test requires re-enabling the workflow AND `allow_restart=true`.
- `report` writes EMAIL_TEST_RESULTS.md from the database.

Uses its own lock and tables; never touches production monitoring state.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .notifier import Outcome, ResendSender
from .repository import S, Repository

log = logging.getLogger(__name__)

LOCK_NAME = "nyrr-email-test"
TOTAL_CYCLES = 4
INTERVAL = timedelta(minutes=15)
WINDOW = timedelta(minutes=60)
EARLY_TOLERANCE = timedelta(minutes=3)
MIN_GAP = timedelta(minutes=10)
SEND_RETRY_DELAYS = (30, 60)  # seconds; 3 attempts total per recipient per cycle


class EmailTestError(Exception):
    pass


@dataclass
class TickResult:
    action: str          # started | sent | not_due | no_active_test | finished
    cycle: int | None = None
    finished: bool = False
    test_id: uuid.UUID | None = None


def render_test_email(cycle: int, executed_at: datetime, tz: ZoneInfo) -> tuple[str, str]:
    subject = f"NYRR MONITOR — TEST [{cycle}/{TOTAL_CYCLES}]"
    body = "\n".join([
        "NYRR 9+1 Volunteer Monitor — Email Delivery Test",
        f"Test number: {cycle}",
        f"Execution time: {executed_at.astimezone(tz).strftime('%Y-%m-%d %I:%M:%S %p %Z')}",
        "Cloud execution status: SUCCESS",
        "Email notification system: TEST MODE",
        "This is an automated test of the NYRR volunteer monitoring system.",
        "No actual volunteer opportunity has been detected or verified.",
        "No action is required.",
    ])
    return subject, body


def _active(repo: Repository) -> dict | None:
    return repo._one(f"SELECT * FROM {S}email_test_runs WHERE status = 'ACTIVE' FOR UPDATE")


def start(repo: Repository, sender: ResendSender, recipients: tuple[str, ...], *, authorized_by: str,
          allow_restart: bool, tz: ZoneInfo, trigger: str = "workflow_dispatch",
          now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
          sleep: Callable[[float], None] = time.sleep) -> TickResult:
    now = now_fn()
    holder = str(uuid.uuid4())
    if not repo.acquire_lock(LOCK_NAME, holder, now=now, ttl=timedelta(minutes=12)):
        raise EmailTestError("another email-test execution is running")
    try:
        with repo.tx():
            if _active(repo) is not None:
                raise EmailTestError("an email test is already active; it cannot be restarted while running")
            previous = repo._one(f"SELECT COUNT(*) AS n FROM {S}email_test_runs")["n"]
            if previous and not allow_restart:
                raise EmailTestError("a previous email test exists; restarting requires explicit allow_restart=true")
            test_id = uuid.uuid4()
            repo.conn.execute(
                f"""INSERT INTO {S}email_test_runs (test_id, activated_at, expires_at, status, authorized_by, total_cycles)
                    VALUES (%s, %s, %s, 'ACTIVE', %s, %s)""",
                (test_id, now, now + WINDOW, authorized_by, TOTAL_CYCLES),
            )
        _run_cycle(repo, sender, recipients, test_id, 1, expected_at=now, trigger=trigger, tz=tz,
                   now_fn=now_fn, sleep=sleep)
        log.info("email_test_started", extra={"test_id": str(test_id)})
        return TickResult("started", cycle=1, test_id=test_id)
    finally:
        repo.release_lock(LOCK_NAME, holder)


def tick(repo: Repository, sender: ResendSender, recipients: tuple[str, ...], *, tz: ZoneInfo,
         trigger: str = "schedule", now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
         sleep: Callable[[float], None] = time.sleep) -> TickResult:
    now = now_fn()
    holder = str(uuid.uuid4())
    if not repo.acquire_lock(LOCK_NAME, holder, now=now, ttl=timedelta(minutes=12)):
        return TickResult("locked")
    try:
        with repo.tx():
            test = _active(repo)
            if test is None:
                # "finished" (=> the workflow disables its schedule) only once a test has actually run,
                # so an idle tick between enabling the workflow and starting the test does not stop it.
                any_test = repo._one(f"SELECT COUNT(*) AS n FROM {S}email_test_runs")["n"] > 0
                return TickResult("no_active_test", finished=any_test)
            last = repo._one(
                f"""SELECT cycle_number, executed_at FROM {S}email_test_cycles WHERE test_id = %s
                    ORDER BY cycle_number DESC LIMIT 1""", (test["test_id"],))
            done = last["cycle_number"] if last else 0
            if done >= TOTAL_CYCLES:
                _finish(repo, test["test_id"], "COMPLETED", now)
                return TickResult("finished", finished=True, test_id=test["test_id"])
            if now > test["expires_at"]:
                _finish(repo, test["test_id"], "EXPIRED", now)
                return TickResult("finished", finished=True, test_id=test["test_id"])
            nxt = done + 1
            expected = test["activated_at"] + (nxt - 1) * INTERVAL
            if now < expected - EARLY_TOLERANCE or (last and now - last["executed_at"] < MIN_GAP):
                return TickResult("not_due", test_id=test["test_id"])
        _run_cycle(repo, sender, recipients, test["test_id"], nxt, expected_at=expected, trigger=trigger, tz=tz,
                   now_fn=now_fn, sleep=sleep)
        finished = nxt >= TOTAL_CYCLES
        if finished:
            with repo.tx():
                _finish(repo, test["test_id"], "COMPLETED", now_fn())
        return TickResult("sent", cycle=nxt, finished=finished, test_id=test["test_id"])
    finally:
        repo.release_lock(LOCK_NAME, holder)


def _finish(repo: Repository, test_id: uuid.UUID, status: str, now: datetime) -> None:
    repo.conn.execute(
        f"UPDATE {S}email_test_runs SET status = %s, completed_at = %s WHERE test_id = %s AND status = 'ACTIVE'",
        (status, now, test_id),
    )
    log.info("email_test_finished", extra={"test_id": str(test_id), "status": status})


def _run_cycle(repo, sender, recipients, test_id, cycle, *, expected_at, trigger, tz, now_fn, sleep) -> None:
    executed_at = now_fn()
    cycle_id = uuid.uuid4()
    with repo.tx():
        inserted = repo._one(
            f"""INSERT INTO {S}email_test_cycles (cycle_id, test_id, cycle_number, expected_at, executed_at, trigger)
                VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (test_id, cycle_number) DO NOTHING RETURNING cycle_id""",
            (cycle_id, test_id, cycle, expected_at, executed_at, trigger),
        )
        if inserted is None:
            log.warning("email_test_cycle_already_ran", extra={"cycle": cycle})
            return
        for r in recipients:
            repo.conn.execute(
                f"""INSERT INTO {S}email_test_deliveries (delivery_id, cycle_id, recipient_email, delivery_status)
                    VALUES (%s, %s, %s, 'PENDING')""", (uuid.uuid4(), cycle_id, r))
    subject, body = render_test_email(cycle, executed_at, tz)
    for r in recipients:  # recipient isolation: each has its own row and retries
        key = f"nyrr-test-{test_id}-{cycle}-{hashlib.sha256(r.encode()).hexdigest()[:12]}"
        for attempt in range(1, len(SEND_RETRY_DELAYS) + 2):
            requested = now_fn()
            repo.conn.execute(
                f"""UPDATE {S}email_test_deliveries SET delivery_status = 'SENDING', attempt_count = %s,
                        requested_at = COALESCE(requested_at, %s) WHERE cycle_id = %s AND recipient_email = %s""",
                (attempt, requested, cycle_id, r))
            result = sender.send(to=r, subject=subject, text=body, html_body=None, idempotency_key=key)
            responded = now_fn()
            if result.outcome is Outcome.ACCEPTED:
                status = "ACCEPTED"
            elif result.outcome is Outcome.RETRYABLE and attempt <= len(SEND_RETRY_DELAYS):
                status = "FAILED_RETRYABLE"
            else:
                status = "FAILED_PERMANENT"
            repo.conn.execute(
                f"""UPDATE {S}email_test_deliveries SET delivery_status = %s, responded_at = %s,
                        provider_message_id = COALESCE(%s, provider_message_id), last_error = %s
                    WHERE cycle_id = %s AND recipient_email = %s""",
                (status, responded, result.message_id, result.error, cycle_id, r))
            if status != "FAILED_RETRYABLE":
                break
            sleep(SEND_RETRY_DELAYS[attempt - 1])


# ====================================================================== report
def build_report(repo: Repository, tz: ZoneInfo, test_id: uuid.UUID | None = None) -> str:
    test = repo._one(
        f"SELECT * FROM {S}email_test_runs " + ("WHERE test_id = %s" if test_id else "ORDER BY activated_at DESC LIMIT 1"),
        (test_id,) if test_id else (),
    )
    if test is None:
        return "# EMAIL_TEST_RESULTS\n\nNo email test has been run.\n"
    cycles = repo._all(f"SELECT * FROM {S}email_test_cycles WHERE test_id = %s ORDER BY cycle_number", (test["test_id"],))
    deliveries = repo._all(
        f"""SELECT d.*, c.cycle_number FROM {S}email_test_deliveries d
            JOIN {S}email_test_cycles c ON c.cycle_id = d.cycle_id
            WHERE c.test_id = %s ORDER BY c.cycle_number, d.recipient_email""", (test["test_id"],))

    def f(dt):
        return dt.astimezone(tz).strftime("%Y-%m-%d %I:%M:%S %p %Z") if dt else "—"

    accepted = sum(d["delivery_status"] == "ACCEPTED" for d in deliveries)
    failed = [d for d in deliveries if d["delivery_status"] in ("FAILED_PERMANENT", "FAILED_RETRYABLE", "PENDING", "SENDING")]
    ran = {c["cycle_number"] for c in cycles}
    missed = [n for n in range(1, TOTAL_CYCLES + 1) if n not in ran]
    if test["status"] == "ACTIVE":
        final = "IN PROGRESS"
    elif not missed and accepted == len(deliveries) and deliveries:
        final = "PASSED (provider acceptance) — awaiting recipient confirmation"
    else:
        final = "PARTIAL / FAILED — see details"

    out = [
        "# EMAIL_TEST_RESULTS", "",
        f"- Test ID: `{test['test_id']}`",
        f"- Authorized by: {test['authorized_by']}",
        f"- Test start (activation): {f(test['activated_at'])}",
        f"- Test end: {f(test['completed_at'])} (window closes {f(test['expires_at'])})",
        f"- Test status: {test['status']}",
        f"- Expected executions: {TOTAL_CYCLES} · Actual executions: {len(cycles)}",
        f"- Missed scheduled executions: {', '.join(map(str, missed)) or 'none'}",
        f"- Emails attempted: {len(deliveries)} · Accepted by provider: {accepted} · Rejected/failed: {len(failed)}",
        f"- **Final test status: {final}**", "",
        "> Provider acceptance (Resend HTTP 200 with a message id) is not confirmed inbox delivery.",
        "> End-to-end success requires both recipients to confirm they received all accepted messages.", "",
        "## Executions (expected vs actual)", "",
        "| Cycle | Trigger | Expected (ET) | Actual (ET) | Delay |", "|---|---|---|---|---|",
    ]
    for c in cycles:
        delay = (c["executed_at"] - c["expected_at"]).total_seconds()
        out.append(f"| {c['cycle_number']} | {c['trigger']} | {f(c['expected_at'])} | {f(c['executed_at'])} | {delay/60:+.1f} min |")
    out += ["", "## Deliveries", "",
            "| Cycle | Recipient | Status | Attempts | API latency | Provider message id | Error |",
            "|---|---|---|---|---|---|---|"]
    for d in deliveries:
        lat = (d["responded_at"] - d["requested_at"]).total_seconds() if d["responded_at"] and d["requested_at"] else None
        out.append(f"| {d['cycle_number']} | {d['recipient_email']} | {d['delivery_status']} | {d['attempt_count']} | "
                   f"{'%.2f s' % lat if lat is not None else '—'} | {d['provider_message_id'] or '—'} | {d['last_error'] or ''} |")
    out += ["", "Latency above is API request→response time, not inbox arrival time.", "",
            "## Recipient confirmation", ""]
    for r in sorted({d["recipient_email"] for d in deliveries}):
        out.append(f"- [ ] {r} confirmed receipt of all accepted test emails")
    out.append("")
    return "\n".join(out)
