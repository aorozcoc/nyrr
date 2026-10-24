"""Notification delivery (spec §16 cases 19–25) + Resend client behaviour."""

import json
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from src.notifier import OPENING_SUBJECT, Outcome, ResendSender, dispatch_pending, render_opening_email
from tests.conftest import RECIPIENTS
from tests.test_repository import BAG, observe, opened

TZ = ZoneInfo("America/New_York")
DAVID, ALEJANDRO = RECIPIENTS


def dispatch(repo, sender, clock, *, budget=420, max_attempts=3):
    return dispatch_pending(repo, sender, tz=TZ, retry_delays=(60, 300, 900), max_attempts=max_attempts,
                            evidence_max_age=timedelta(minutes=20), wait_budget_seconds=budget,
                            now_fn=clock, sleep=clock.sleep)


def statuses(repo):
    rows = repo._all("SELECT recipient_email, delivery_status, attempt_count, needs_review, provider_message_id "
                     "FROM nyrr_monitor.notifications")
    return {r["recipient_email"]: r for r in rows}


@pytest.fixture
def pending(repo, clock):
    observe(repo, opened(), clock())
    return repo


def test_19_both_recipients_succeed(pending, sender, clock):
    stats = dispatch(pending, sender, clock)
    st = statuses(pending)
    assert stats.accepted == 2 and {r["delivery_status"] for r in st.values()} == {"ACCEPTED"}
    assert all(r["provider_message_id"] for r in st.values())
    assert sorted(s["to"] for s in sender.sent) == sorted(RECIPIENTS)
    assert all(s["subject"] == OPENING_SUBJECT for s in sender.sent)


def test_20_first_recipient_fails_then_retries_alone(pending, sender, clock):
    first = sorted(RECIPIENTS)[0]
    sender.script[first] = [Outcome.RETRYABLE]
    dispatch(pending, sender, clock)
    st = statuses(pending)
    assert st[first]["delivery_status"] == "ACCEPTED" and st[first]["attempt_count"] == 2
    other = [r for r in RECIPIENTS if r != first][0]
    assert st[other]["attempt_count"] == 1  # the successful recipient is never re-sent
    assert [s["to"] for s in sender.sent].count(other) == 1
    assert 60 in clock.sleeps  # retried after 1 minute, inside the same job


def test_21_second_recipient_fails_permanently_first_preserved(pending, sender, clock):
    second = sorted(RECIPIENTS)[1]
    sender.script[second] = [Outcome.PERMANENT]
    stats = dispatch(pending, sender, clock)
    st = statuses(pending)
    assert st[sorted(RECIPIENTS)[0]]["delivery_status"] == "ACCEPTED"
    assert st[second]["delivery_status"] == "FAILED_PERMANENT" and st[second]["needs_review"] is True
    assert stats.permanent_failures == 1
    n = pending._one("SELECT COUNT(*) AS n FROM nyrr_monitor.availability_transitions WHERE shift_id=%s", (BAG,))["n"]
    assert n == 1  # no new availability transition was created


def test_22_provider_timeout_reconciles_via_idempotency_key(pending, clock):
    calls = []

    def handler(request):
        calls.append((request.headers["Idempotency-Key"], json.loads(request.content)["to"][0]))
        if len(calls) == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"id": f"msg-{len(calls)}"})

    sender = ResendSender("re_test_123456789", "Monitor <a@example.org>", transport=httpx.MockTransport(handler))
    dispatch(pending, sender, clock)
    assert {r["delivery_status"] for r in statuses(pending).values()} == {"ACCEPTED"}
    timed_out_key, timed_out_to = calls[0]
    retry = [c for c in calls[1:] if c[1] == timed_out_to]
    assert retry and retry[0][0] == timed_out_key  # same key -> provider replays, no duplicate email


def test_23_accepted_is_not_reported_as_delivered(pending, sender, clock):
    dispatch(pending, sender, clock)
    rows = pending._all("SELECT delivery_status, accepted_at FROM nyrr_monitor.notifications")
    assert all(r["delivery_status"] == "ACCEPTED" and r["accepted_at"] for r in rows)
    with pytest.raises(Exception):  # there is deliberately no DELIVERED state
        pending.conn.execute("UPDATE nyrr_monitor.notifications SET delivery_status = 'DELIVERED'")


def test_24_duplicate_attempts_do_not_duplicate_emails(pending, sender, clock):
    dispatch(pending, sender, clock)
    dispatch(pending, sender, clock)  # second dispatcher pass: nothing due
    assert len(sender.sent) == 2
    # crash mid-send: row stuck in SENDING after the provider already accepted it
    pending.conn.execute("""UPDATE nyrr_monitor.notifications SET delivery_status = 'SENDING',
                            last_attempt_at = %s""", (clock() - timedelta(minutes=11),))
    dispatch(pending, sender, clock)
    assert len(sender.sent) == 4  # re-sent with the same keys...
    assert len(sender.delivered()) == 2  # ...so recipients still receive exactly one email each


def test_25_retry_exhaustion_flags_for_review(pending, sender, clock):
    for r in RECIPIENTS:
        sender.script[r] = [Outcome.RETRYABLE] * 3
    stats = dispatch(pending, sender, clock)
    st = statuses(pending)
    assert all(r["delivery_status"] == "FAILED_PERMANENT" and r["attempt_count"] == 3 and r["needs_review"]
               for r in st.values())
    assert stats.permanent_failures == 2
    assert 60 in clock.sleeps and 300 in clock.sleeps
    assert len(pending.notifications_needing_review()) == 2


def test_retry_beyond_budget_is_left_for_next_run(pending, sender, clock):
    for r in RECIPIENTS:
        sender.script[r] = [Outcome.RETRYABLE, Outcome.RETRYABLE]
    dispatch(pending, sender, clock, budget=120)  # waits for the 1-min retry, not the 5-min one
    st = statuses(pending)
    assert all(r["delivery_status"] == "FAILED_RETRYABLE" and r["attempt_count"] == 2 for r in st.values())


def test_stale_availability_is_suppressed_not_sent(pending, sender, clock):
    clock.advance(minutes=25)
    stats = dispatch(pending, sender, clock)
    assert stats.suppressed == 2 and sender.sent == []
    assert {r["delivery_status"] for r in statuses(pending).values()} == {"SUPPRESSED"}


def test_closed_before_send_is_suppressed(pending, sender, clock):
    from tests.conftest import fixture_text
    observe(pending, fixture_text("event_race_to_deliver_2026-10-24.html"), clock() + timedelta(seconds=5))
    clock.advance(seconds=10)
    dispatch(pending, sender, clock)
    assert sender.sent == []


# ---------------------------------------------------------------- Resend client
def _resend(handler):
    return ResendSender("re_test_123456789", "NYRR Monitor <alerts@example.org>", transport=httpx.MockTransport(handler))


def test_resend_request_shape():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["Authorization"]
        seen["key"] = request.headers["Idempotency-Key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "abc"})

    r = _resend(handler).send(to="x@example.com", subject="s", text="t", html_body="<p>h</p>", idempotency_key="k1")
    assert r.outcome is Outcome.ACCEPTED and r.message_id == "abc"
    assert seen["auth"] == "Bearer re_test_123456789" and seen["key"] == "k1"
    assert seen["body"]["to"] == ["x@example.com"] and seen["body"]["from"].endswith("<alerts@example.org>")


@pytest.mark.parametrize("status,body,expected", [
    (429, {"name": "rate_limit_exceeded", "message": "slow"}, Outcome.RETRYABLE),
    (500, {"name": "internal_server_error", "message": "x"}, Outcome.RETRYABLE),
    (409, {"name": "concurrent_idempotent_requests", "message": "x"}, Outcome.RETRYABLE),
    (409, {"name": "invalid_idempotent_request", "message": "x"}, Outcome.PERMANENT),
    (422, {"name": "validation_error", "message": "bad from"}, Outcome.PERMANENT),
    (401, {"name": "missing_api_key", "message": "x"}, Outcome.PERMANENT),
    (200, {}, Outcome.RETRYABLE),  # 200 without an id is not trusted as acceptance
])
def test_resend_error_classification(status, body, expected):
    r = _resend(lambda req: httpx.Response(status, json=body)).send(
        to="x@example.com", subject="s", text="t", html_body=None, idempotency_key="k")
    assert r.outcome is expected


def test_opening_email_contains_every_required_field(pending, clock):
    jobs = pending.claim_due_notifications(now=clock())
    subject, text, html = render_opening_email(jobs[0], TZ)
    assert subject == "NYRR 9+1 ALERT — VERIFIED VOLUNTEER OPENING"
    for needle in ["Race to Deliver 4M", "Sunday, November 22, 2026", "Central Park", "Bag Check",
                   "6:00 AM EST to 10:30 AM EST", "2026 +1 credit: YES", "Registration:   OPEN",
                   "https://register.nyrr.org/?event=76745eea47f712e75d62&option=bag",
                   "Verified at:    Fri Oct 23, 2026 9:15 PM EDT", "Availability can change"]:
        assert needle in text, needle
    assert "register.nyrr.org" in html and "Availability can change" in html
    # deterministic: identical render on retry (required for idempotent resend)
    assert render_opening_email(jobs[0], TZ) == (subject, text, html)
    assert jobs[0].event_date == date(2026, 11, 22)


def test_review7_email_content_frozen_at_detection(pending, clock):
    """Re-observations must not change the e-mail sent under the same idempotency key."""
    clock.advance(minutes=5)
    observe(pending, opened(), clock())  # still open: verified_at moves forward, no new transition
    jobs = pending.claim_due_notifications(now=clock())
    _, text, _ = render_opening_email(jobs[0], TZ)
    assert "Verified at:    Fri Oct 23, 2026 9:15 PM EDT" in text  # detection time, not 9:20
    assert jobs[0].shift_verified_at > jobs[0].verified_at
