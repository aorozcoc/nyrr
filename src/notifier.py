"""Email delivery through Resend, plus the outbox dispatcher (spec §14).

Delivery guarantees
- One email per recipient per qualifying transition (outbox rows are unique per pair).
- Every send carries a Resend `Idempotency-Key` derived from (transition, recipient). Resend keeps keys
  for 24 h and replays the original response for an identical request, so a retry after an ambiguous
  outcome (timeout, crash mid-send) reconciles with the provider instead of sending a duplicate.
- Recipient isolation: each recipient has its own row, status and retries.
- "ACCEPTED" means Resend accepted the message. It is NOT proof of inbox delivery.
- Emails are never sent while a database transaction is open.
"""

from __future__ import annotations

import hashlib
import html
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

import httpx

from .models import DeliveryStatus
from .repository import AVAILABLE, NotificationJob, Repository

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
OPENING_SUBJECT = "NYRR 9+1 ALERT — VERIFIED VOLUNTEER OPENING"
TEST_SUBJECT = "NYRR MONITOR — TEST NOTIFICATION"
TEST_BODY = ("This is a test of the NYRR 9+1 volunteer monitoring system. "
             "No actual volunteer opening has been detected.")
HEALTH_SUBJECT = "NYRR MONITOR — SYSTEM HEALTH ALERT"


class Outcome(StrEnum):
    ACCEPTED = "accepted"
    RETRYABLE = "retryable"      # includes ambiguous outcomes (timeouts); safe due to idempotency key
    PERMANENT = "permanent"


@dataclass(frozen=True)
class SendResult:
    outcome: Outcome
    message_id: str | None = None
    error: str | None = None
    http_status: int | None = None


class ResendSender:
    def __init__(self, api_key: str, sender: str, *, transport: httpx.BaseTransport | None = None,
                 timeout: float = 15.0) -> None:
        self.sender = sender
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "nyrr-9plus1-monitor/1.0"},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def send(self, *, to: str, subject: str, text: str, html_body: str | None, idempotency_key: str) -> SendResult:
        payload = {"from": self.sender, "to": [to], "subject": subject, "text": text}
        if html_body:
            payload["html"] = html_body
        try:
            r = self._client.post(RESEND_URL, json=payload, headers={"Idempotency-Key": idempotency_key[:256]})
        except httpx.TimeoutException:
            return SendResult(Outcome.RETRYABLE, error="timeout (outcome unknown; will reconcile via idempotency key)")
        except httpx.HTTPError as exc:
            return SendResult(Outcome.RETRYABLE, error=f"network error: {type(exc).__name__}")
        status = r.status_code
        detail = _error_detail(r)
        if status == 200:
            message_id = (r.json() or {}).get("id") if _is_json(r) else None
            if not message_id:
                return SendResult(Outcome.RETRYABLE, error="200 without message id", http_status=status)
            return SendResult(Outcome.ACCEPTED, message_id=message_id, http_status=status)
        if status == 409 and "concurrent" in detail:
            return SendResult(Outcome.RETRYABLE, error=f"409 {detail}", http_status=status)
        if status == 429 or status >= 500:
            return SendResult(Outcome.RETRYABLE, error=f"{status} {detail}", http_status=status)
        return SendResult(Outcome.PERMANENT, error=f"{status} {detail}", http_status=status)


class GmailSender:
    """Send through Gmail SMTP (smtp.gmail.com:465, implicit TLS) with an app password.

    SMTP has no idempotency keys: if the connection drops after Gmail accepted a message but before
    we saw the reply, a retry can deliver a duplicate. That window is small and is the accepted
    trade-off of the lite setup (no Resend account).
    """

    HOST, PORT = "smtp.gmail.com", 465

    def __init__(self, address: str, app_password: str, *, smtp_factory=None, timeout: float = 20.0) -> None:
        import smtplib

        self.address = address
        self._password = app_password.replace(" ", "")  # Google shows app passwords in groups of 4
        self._timeout = timeout
        self._smtp_factory = smtp_factory or (lambda: smtplib.SMTP_SSL(self.HOST, self.PORT, timeout=timeout))

    def close(self) -> None:
        pass

    def send(self, *, to: str, subject: str, text: str, html_body: str | None, idempotency_key: str) -> SendResult:
        import smtplib
        import socket
        from email.message import EmailMessage
        from email.utils import make_msgid

        msg = EmailMessage()
        msg["From"] = f"NYRR 9+1 Monitor <{self.address}>"
        msg["To"] = to
        msg["Subject"] = subject
        domain = self.address.split("@", 1)[-1]
        message_id = make_msgid(idstring=hashlib.sha256(idempotency_key.encode()).hexdigest()[:16], domain=domain)
        msg["Message-ID"] = message_id
        msg.set_content(text)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        try:
            with self._smtp_factory() as smtp:
                smtp.login(self.address, self._password)
                refused = smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError:
            return SendResult(Outcome.PERMANENT, error="Gmail rejected the login (check GMAIL_APP_PASSWORD)")
        except smtplib.SMTPRecipientsRefused:
            return SendResult(Outcome.PERMANENT, error="Gmail refused the recipient address")
        except smtplib.SMTPResponseException as exc:
            kind = Outcome.RETRYABLE if 400 <= exc.smtp_code < 500 else Outcome.PERMANENT
            return SendResult(kind, error=f"SMTP {exc.smtp_code}")
        except (smtplib.SMTPException, OSError, socket.timeout) as exc:
            return SendResult(Outcome.RETRYABLE, error=f"SMTP connection problem: {type(exc).__name__}")
        if refused:
            return SendResult(Outcome.PERMANENT, error="Gmail refused the recipient address")
        return SendResult(Outcome.ACCEPTED, message_id=message_id)


def _is_json(r: httpx.Response) -> bool:
    return "json" in r.headers.get("content-type", "")


def _error_detail(r: httpx.Response) -> str:
    if r.status_code == 200:
        return ""
    try:
        body = r.json()
        return f"{body.get('name', '')}: {body.get('message', '')}"[:300]
    except Exception:
        return r.text[:200]


# ====================================================================== rendering
def _fmt_dt(dt: datetime | None, tz: ZoneInfo) -> str:
    if dt is None:
        return "see NYRR page"
    local = dt.astimezone(tz)
    return f"{local:%a %b} {local.day}, {local.year} {_clock(local)} {local.tzname()}"


def _clock(dt: datetime) -> str:
    """'6:00 AM' — portable (no platform-specific %-I)."""
    return f"{dt.hour % 12 or 12}:{dt:%M %p}"


def _long_date(d) -> str:
    return f"{d:%A, %B} {d.day}, {d.year}"


def render_opening_email(job: NotificationJob, tz: ZoneInfo) -> tuple[str, str, str]:
    """Deterministic for a given job (required for idempotent retries)."""
    when = _long_date(job.event_date)
    if job.event_end_date and job.event_end_date != job.event_date:
        when += " – " + _long_date(job.event_end_date)
    shift_window = _fmt_dt(job.start_time, tz)
    if job.end_time:
        end_local = job.end_time.astimezone(tz)
        shift_window += f" to {_clock(end_local)} {end_local.tzname()}"
    verified = _fmt_dt(job.verified_at, tz)
    lines = [
        "A volunteer shift that qualifies for 2026 9+1 credit is open for registration.",
        "",
        f"Event:          {job.event_name}",
        f"Event date:     {when}",
        f"Location:       {job.location or 'see NYRR page'}",
        f"Role:           {job.role_name}",
        f"Shift time:     {shift_window}",
        "2026 +1 credit: YES — shift tagged 9+1 on a 2026 event (non-medical, not restricted)",
        f"Registration:   OPEN — shift status '{job.availability_status}' with an active Register link",
        f"Register here:  {job.registration_url}",
        f"Event page:     {job.event_url}",
        f"Verified at:    {verified}",
        "",
        "Availability can change at any moment; the shift may fill before you register.",
        "This monitor never registers for you. Confirm the details on nyrr.org before signing up.",
    ]
    text = "\n".join(lines)
    e = html.escape
    rows = "".join(
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>{e(k)}</td><td style='padding:4px 0'>{v}</td></tr>"
        for k, v in [
            ("Event", e(job.event_name)), ("Event date", e(when)), ("Location", e(job.location or "see NYRR page")),
            ("Role", e(job.role_name)), ("Shift time", e(shift_window)),
            ("2026 +1 credit", "<b>YES</b> — tagged 9+1, 2026 event, non-medical, not restricted"),
            ("Registration", f"<b>OPEN</b> — status “{e(job.availability_status)}” with an active Register link"),
            ("Verified at", e(verified)),
        ]
    )
    html_body = (
        "<div style='font-family:Arial,sans-serif;font-size:15px;color:#111'>"
        "<p>A volunteer shift that qualifies for <b>2026 9+1 credit</b> is open for registration.</p>"
        f"<table style='border-collapse:collapse'>{rows}</table>"
        f"<p><a href='{e(job.registration_url or '')}' style='display:inline-block;background:#0b5;color:#fff;"
        "padding:10px 16px;text-decoration:none;border-radius:4px'>Register on NYRR</a></p>"
        f"<p><a href='{e(job.event_url)}'>Event page</a></p>"
        "<p style='color:#a00'><b>Availability can change at any moment</b>; the shift may fill before you register. "
        "This monitor never registers for you.</p></div>"
    )
    return OPENING_SUBJECT, text, html_body


# ====================================================================== dispatcher
@dataclass
class DispatchStats:
    accepted: int = 0
    retry_scheduled: int = 0
    permanent_failures: int = 0
    suppressed: int = 0
    provider_errors: list[str] | None = None


def dispatch_pending(
    repo: Repository,
    sender: ResendSender,
    *,
    tz: ZoneInfo,
    retry_delays: tuple[int, ...],
    max_attempts: int,
    evidence_max_age: timedelta,
    wait_budget_seconds: int,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    resolve_recipient: Callable[[str], str | None] = lambda r: r,
) -> DispatchStats:
    """Send due outbox rows; wait in-process for short retries (1 min, 5 min) within the budget.

    `resolve_recipient` maps the stored recipient key to an e-mail address. The monitor stores
    pseudonymous keys ("recipient-1") so the database never holds addresses (it is published as
    state in a public repository in the lite setup)."""
    stats = DispatchStats(provider_errors=[])
    deadline = now_fn() + timedelta(seconds=wait_budget_seconds)
    repo.recover_stale_sending(now=now_fn())
    while True:
        jobs = repo.claim_due_notifications(now=now_fn())
        for job in jobs:
            _deliver_one(repo, sender, job, tz=tz, retry_delays=retry_delays, max_attempts=max_attempts,
                         evidence_max_age=evidence_max_age, now_fn=now_fn, stats=stats,
                         resolve_recipient=resolve_recipient)
        due = repo.next_retry_due()
        if due is None:
            break
        now = now_fn()
        if due <= now:
            if not jobs:  # due rows exist but are held by another process; leave them to it
                break
            continue
        if due > deadline:
            log.info("dispatch_retry_deferred_to_next_run", extra={"next_attempt_at": due.isoformat()})
            break
        sleep((due - now).total_seconds())
    return stats


def _deliver_one(repo: Repository, sender: ResendSender, job: NotificationJob, *, tz: ZoneInfo,
                 retry_delays: tuple[int, ...], max_attempts: int, evidence_max_age: timedelta,
                 now_fn: Callable[[], datetime], stats: DispatchStats,
                 resolve_recipient: Callable[[str], str | None] = lambda r: r) -> None:
    now = now_fn()
    stale = (
        job.shift_classification != AVAILABLE
        or not job.shift_is_latest_transition
        or job.shift_verified_at is None
        or now - job.shift_verified_at > evidence_max_age
    )
    if stale:
        repo.mark_notification(job.notification_id, DeliveryStatus.SUPPRESSED, now=now,
                               error="availability no longer fresh/verified at send time; not sent")
        stats.suppressed += 1
        rearmed = repo.rearm_if_never_delivered(job.transition_id)
        log.info("notification_suppressed_stale", extra={"notification_id": str(job.notification_id),
                                                         "shift_rearmed": rearmed})
        return

    subject, text, html_body = render_opening_email(job, tz)
    address = resolve_recipient(job.recipient_email)
    if not address:
        repo.mark_notification(job.notification_id, DeliveryStatus.FAILED_PERMANENT, now=now, needs_review=True,
                               error=f"no address configured for {job.recipient_email}")
        stats.permanent_failures += 1
        return
    result = sender.send(to=address, subject=subject, text=text, html_body=html_body,
                         idempotency_key=job.idempotency_key)
    now = now_fn()
    if result.outcome is Outcome.ACCEPTED:
        repo.mark_notification(job.notification_id, DeliveryStatus.ACCEPTED, now=now, message_id=result.message_id)
        stats.accepted += 1
        log.info("notification_accepted", extra={"notification_id": str(job.notification_id),
                                                 "provider_message_id": result.message_id})
        return
    stats.provider_errors.append(result.error or "unknown")
    if result.outcome is Outcome.RETRYABLE and job.attempt_count < max_attempts:
        delay = retry_delays[min(job.attempt_count - 1, len(retry_delays) - 1)]
        repo.mark_notification(job.notification_id, DeliveryStatus.FAILED_RETRYABLE, now=now, error=result.error,
                               next_attempt_at=now + timedelta(seconds=delay))
        stats.retry_scheduled += 1
        log.warning("notification_retry_scheduled", extra={"notification_id": str(job.notification_id),
                                                           "attempt": job.attempt_count, "delay_s": delay,
                                                           "error": result.error})
    else:
        repo.mark_notification(job.notification_id, DeliveryStatus.FAILED_PERMANENT, now=now, error=result.error,
                               needs_review=True)
        stats.permanent_failures += 1
        log.error("notification_failed_permanently", extra={"notification_id": str(job.notification_id),
                                                            "attempt": job.attempt_count, "error": result.error})
