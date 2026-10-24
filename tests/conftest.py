"""Shared fixtures.

Database tests run against a real PostgreSQL given by TEST_DATABASE_URL (a superuser/owner URL on a
throw-away database). Each test gets a freshly migrated `nyrr_monitor` schema. Without the variable,
database tests are skipped (CI always sets it).

NYRR is never contacted: a fake site serves the captured fixture pages through httpx.MockTransport.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from src.config import Settings
from src.notifier import Outcome, SendResult

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
MIGRATIONS = ROOT / "db" / "migrations"
BASE = "https://prod.nyrr.org"
RTD = "/races-and-events/2026/race-to-deliver-4m-to-benefit-gods-love-we-deliver-volunteers"
MIDNIGHT = "/races-and-events/2026/nyrr-midnight-run-volunteers"
MEDICAL = "/races-and-events/2026/tcs-new-york-city-marathon-medical-volunteers"
CALENDAR = "/get-involved-volunteer-opportunities"
RECIPIENTS = ("recipient.one@example.com", "recipient.two@example.com")  # real addresses live only in secrets
# Captured at 2026-10-24 01:15 UTC (2026-10-23 21:15 ET)
T0 = datetime(2026, 10, 24, 1, 15, tzinfo=UTC)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- clock
class FakeClock:
    def __init__(self, start: datetime = T0):
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)

    def monotonic(self) -> float:
        return self.now.timestamp()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# ---------------------------------------------------------------- fake NYRR site
def open_shift(html: str, option_id: str, register_url: str | None = None, status: str = "available") -> str:
    """Return page HTML with one shift flipped to `status`, adding a Register link if opening."""
    pattern = re.compile(
        r'(<article data-registration-option-id="' + re.escape(option_id) + r'">.*?</article>)', re.S)
    m = pattern.search(html)
    assert m, f"option {option_id} not in fixture"
    article = m.group(1)
    article = re.sub(r'data-event-status="[^"]*">[^<]*<', f'data-event-status="{status}">{status.title()}<', article, count=1)
    article = re.sub(r'<a href="https://register\.nyrr\.org[^"]*"[^>]*>.*?</a>', "", article, flags=re.S)
    if status == "available" and register_url is not None:
        # same position as on the live page: after the details block, inside the shift body
        assert article.endswith("</ul></div></div></article>")
        article = article[: -len("</div></article>")] + (
            f'<a href="{register_url}" target="_blank" data-link-is-button="true"><span>Register</span></a>'
            "</div></article>")
    return html[: m.start()] + article + html[m.end():]


def synthetic_event_page(path: str) -> str:
    """A realistic page for events without a captured fixture: Race to Deliver's layout, every +1 shift
    sold out, shift ids made unique per event."""
    import hashlib

    tag = hashlib.sha256(path.encode()).hexdigest()[:10]
    html = fixture_text("event_race_to_deliver_2026-10-24.html")
    html = html.replace("6ad79c5e3fa07f0001af", tag)
    html = html.replace("/races-and-events/2026/race-to-deliver-4m-to-benefit-gods-love-we-deliver-volunteers", path)
    return html


@dataclass
class FakeSite:
    pages: dict[str, str] = field(default_factory=dict)
    status_overrides: dict[str, int] = field(default_factory=dict)
    headers_overrides: dict[str, dict] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)
    fail_once: dict[str, int] = field(default_factory=dict)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        if self.fail_once.get(path):
            self.fail_once[path] -= 1
            return httpx.Response(503, text="unavailable")
        if path in self.status_overrides:
            return httpx.Response(self.status_overrides[path], headers=self.headers_overrides.get(path, {}), text="x")
        if path not in self.pages:
            if path.startswith("/races-and-events/2026/"):
                return httpx.Response(200, headers={"content-type": "text/html"}, text=synthetic_event_page(path))
            return httpx.Response(404, text="not found")
        return httpx.Response(200, headers={"content-type": "text/html"}, text=self.pages[path])

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def site() -> FakeSite:
    return FakeSite(pages={
        CALENDAR: fixture_text("calendar_2026-10-24.html"),
        RTD: fixture_text("event_race_to_deliver_2026-10-24.html"),
        MIDNIGHT: fixture_text("event_midnight_run_2026-10-24.html"),
        MEDICAL: fixture_text("event_medical_2026-10-24.html"),
    })


# ---------------------------------------------------------------- fake email provider
class FakeSender:
    def __init__(self):
        self.sent: list[dict] = []
        self.script: dict[str, list[Outcome]] = {}  # recipient -> queued outcomes
        self.accepted_keys: dict[str, str] = {}
        self.closed = False

    def send(self, *, to, subject, text, html_body, idempotency_key) -> SendResult:
        queue = self.script.get(to)
        outcome = queue.pop(0) if queue else Outcome.ACCEPTED
        self.sent.append({"to": to, "subject": subject, "text": text, "key": idempotency_key, "outcome": outcome})
        if outcome is Outcome.ACCEPTED:
            # emulate Resend idempotency: same key -> same id, no second delivery
            mid = self.accepted_keys.setdefault(idempotency_key, f"msg_{len(self.accepted_keys) + 1}")
            return SendResult(Outcome.ACCEPTED, message_id=mid, http_status=200)
        if outcome is Outcome.RETRYABLE:
            return SendResult(Outcome.RETRYABLE, error="503 service unavailable", http_status=503)
        return SendResult(Outcome.PERMANENT, error="422 validation_error", http_status=422)

    def delivered(self) -> list[dict]:
        """Unique deliveries (as a recipient would see them) honouring idempotency keys."""
        seen, out = set(), []
        for s in self.sent:
            if s["outcome"] is Outcome.ACCEPTED and s["key"] not in seen:
                seen.add(s["key"])
                out.append(s)
        return out

    def close(self):
        self.closed = True


@pytest.fixture
def sender() -> FakeSender:
    return FakeSender()


# ---------------------------------------------------------------- settings
def make_settings(**overrides) -> Settings:
    base = dict(
        database_url=os.environ.get("TEST_DATABASE_URL", "postgresql://unused"),
        resend_api_key="re_test_key_123456",
        alert_email_from="NYRR Monitor <alerts@example.org>",
        alert_email_to=RECIPIENTS,
        app_env="test",
        nyrr_collection_authorized=True,
        alerts_enabled=True,
        request_delay_seconds=2.0,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


# ---------------------------------------------------------------- database
@pytest.fixture(scope="session")
def db_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set; database tests skipped")
    return url


def reset_schema(url: str) -> None:
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS nyrr_monitor CASCADE")
        conn.execute((MIGRATIONS / "001_initial_schema.sql").read_text())


@pytest.fixture
def db(db_url):
    reset_schema(db_url)
    from src.repository import connect

    conns = []

    def factory(url: str = db_url):
        c = connect(url)
        conns.append(c)
        return c

    yield factory
    for c in conns:
        if not c.closed:
            c.close()


@pytest.fixture
def repo(db):
    from src.repository import Repository

    return Repository(db())


def as_json(value) -> str:
    return json.dumps(value, default=str)
