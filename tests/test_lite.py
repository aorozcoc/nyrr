"""Lite setup: Gmail delivery, recipient pseudonymisation, freshness check, git-branch state store."""

import os
import shutil
import smtplib
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.config import ConfigError, Settings
from src.health import CRITICAL, DEGRADED, HEALTHY, INACTIVE, freshness_status
from src.main import main
from src.notifier import GmailSender, Outcome
from tests.conftest import RECIPIENTS, T0
from tests.test_monitor import Harness

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("America/New_York")
END = date(2026, 12, 31)
APP_PW = "abcd efgh ijkl mnop"


# ---------------------------------------------------------------- Gmail
class FakeSMTP:
    def __init__(self, behaviour=None):
        self.behaviour = behaviour
        self.logins, self.messages = [], []

    def __enter__(self):
        if self.behaviour == "connect":
            raise OSError("network unreachable")
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        if self.behaviour == "auth":
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        self.logins.append((user, pw))

    def send_message(self, msg):
        if self.behaviour == "421":
            raise smtplib.SMTPResponseException(421, b"try later")
        if self.behaviour == "refused":
            raise smtplib.SMTPRecipientsRefused({msg["To"]: (550, b"no such user")})
        self.messages.append(msg)
        return {}


def gmail(behaviour=None):
    fake = FakeSMTP(behaviour)
    return GmailSender("monitor.sender@gmail.com", APP_PW, smtp_factory=lambda: fake), fake


def test_gmail_sends_text_and_html():
    sender, fake = gmail()
    r = sender.send(to="x@example.com", subject="S", text="plain", html_body="<p>h</p>", idempotency_key="k1")
    assert r.outcome is Outcome.ACCEPTED and r.message_id.startswith("<")
    assert fake.logins == [("monitor.sender@gmail.com", "abcdefghijklmnop")]  # spaces stripped
    msg = fake.messages[0]
    assert msg["To"] == "x@example.com" and msg["Subject"] == "S"
    assert msg["From"] == "NYRR 9+1 Monitor <monitor.sender@gmail.com>"
    assert msg.is_multipart() and "plain" in msg.get_body(("plain",)).get_content()
    assert r.message_id.endswith("@gmail.com>")


@pytest.mark.parametrize("behaviour,expected", [("auth", Outcome.PERMANENT), ("refused", Outcome.PERMANENT),
                                                ("421", Outcome.RETRYABLE), ("connect", Outcome.RETRYABLE)])
def test_gmail_error_classification(behaviour, expected):
    sender, _ = gmail(behaviour)
    r = sender.send(to="x@example.com", subject="S", text="t", html_body=None, idempotency_key="k")
    assert r.outcome is expected and APP_PW.replace(" ", "") not in (r.error or "")


# ---------------------------------------------------------------- config
GOOD = {"DATABASE_URL": "postgresql://postgres:x@localhost:5432/postgres", "EMAIL_PROVIDER": "gmail",
        "GMAIL_APP_PASSWORD": APP_PW, "ALERT_EMAIL_FROM": "monitor.sender@gmail.com",
        "ALERT_EMAIL_TO": "recipient.one@example.com,recipient.two@example.com", "APP_ENV": "production"}


def test_gmail_config_valid():
    s = Settings.from_env(GOOD)
    s.validate_for()
    assert s.email_provider == "gmail" and APP_PW in s.secret_values()


@pytest.mark.parametrize("key,value,fragment", [
    ("GMAIL_APP_PASSWORD", "", "GMAIL_APP_PASSWORD is missing"),
    ("GMAIL_APP_PASSWORD", "my-normal-password", "16-character"),
    ("ALERT_EMAIL_FROM", "alerts@example.org", "must be the Gmail address"),
    ("EMAIL_PROVIDER", "carrier-pigeon", "EMAIL_PROVIDER"),
])
def test_gmail_config_invalid(key, value, fragment):
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({**GOOD, key: value}).validate_for()
    assert fragment in str(exc.value) and APP_PW not in str(exc.value)


def test_recipient_keys_resolve():
    s = Settings.from_env(GOOD)
    assert s.recipient_keys == ("recipient-1", "recipient-2")
    assert s.resolve_recipient("recipient-2") == "recipient.two@example.com"
    assert s.resolve_recipient("recipient-3") is None


# ---------------------------------------------------------------- pseudonymised outbox
def test_database_never_stores_addresses_but_both_recipients_are_emailed(db, site, sender, clock):
    h = Harness(db, site, sender, clock)
    h.set_bag("available")
    h.run()
    stored = {r["recipient_email"] for r in h.repo._all("SELECT recipient_email FROM nyrr_monitor.notifications")}
    assert stored == {"recipient-1", "recipient-2"}
    assert sorted(a["to"] for a in h.alerts()) == sorted(RECIPIENTS)
    dump = " ".join(str(v) for row in h.repo._all("SELECT * FROM nyrr_monitor.notifications") for v in row.values())
    assert not any(addr in dump for addr in RECIPIENTS)


# ---------------------------------------------------------------- freshness
@pytest.mark.parametrize("age,status,alert", [(10, HEALTHY, False), (30, DEGRADED, False), (46, CRITICAL, True),
                                              (100, CRITICAL, True), (110, CRITICAL, False)])
def test_freshness_status(age, status, alert):
    s, a, _ = freshness_status(T0 - timedelta(minutes=age), now=T0, end_date=END, tz=TZ)
    assert (s, a) == (status, alert)


def test_freshness_inactive_before_first_state_and_after_end():
    assert freshness_status(None, now=T0, end_date=END, tz=TZ)[0] == INACTIVE
    jan2 = datetime(2027, 1, 2, 12, tzinfo=UTC)
    assert freshness_status(jan2 - timedelta(hours=5), now=jan2, end_date=END, tz=TZ)[0] == INACTIVE


def test_freshness_cli_alerts_once_and_fails(monkeypatch, sender):
    import src.monitor
    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(src.monitor, "_default_sender", lambda s: sender)
    now = datetime.now(UTC)
    assert main(["freshness", "--last-state-epoch", str(int((now - timedelta(minutes=10)).timestamp()))]) == 0
    assert sender.sent == []
    assert main(["freshness", "--last-state-epoch", str(int((now - timedelta(minutes=60)).timestamp()))]) == 1
    assert len(sender.sent) == 2 and sender.sent[0]["subject"] == "NYRR MONITOR — SYSTEM HEALTH ALERT"
    assert "not a volunteer opening" in sender.sent[0]["text"]
    assert main(["freshness"]) == 0  # no state yet -> inactive


# ---------------------------------------------------------------- state branch round trip
def _pg_major(cmd, url=None):
    out = subprocess.run([cmd, "--version"], capture_output=True, text=True).stdout
    return out.split()[-1].split(".")[0] if out else None


@pytest.mark.skipif(not shutil.which("pg_dump") or not shutil.which("git"), reason="needs pg_dump and git")
def test_state_survives_save_and_restore_through_git(db_url, db, site, sender, clock, tmp_path):
    import psycopg
    with psycopg.connect(db_url) as c:
        server_major = str(c.info.server_version // 10000)
    if _pg_major("pg_dump") != server_major:
        pytest.skip("pg_dump client major version differs from the test server")

    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    shutil.copytree(ROOT / "db", work / "db")
    shutil.copytree(ROOT / "scripts", work / "scripts")
    env = {**os.environ, "PGURL": db_url}
    env.pop("PG_CONTAINER", None)

    def store(cmd):
        return subprocess.run(["bash", "scripts/state_store.sh", cmd], cwd=work, env=env, check=True,
                              capture_output=True, text=True).stdout

    # run 1: opening detected and alerted, state saved
    h = Harness(db, site, sender, clock)
    h.set_bag("available")
    h.run()
    assert len(h.alerts()) == 2
    assert "state saved" in store("save")

    # new job: empty database, state restored from git
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute("DROP SCHEMA nyrr_monitor CASCADE")
    assert "state restored" in store("restore")
    h2 = Harness(db, site, sender, clock)
    assert h2.shift()["classification"] == "AVAILABLE_VERIFIED" and h2.shift()["alert_armed"] is False
    h2.set_bag("available")
    h2.run()
    assert len(sender.delivered()) == 2  # still open: no duplicate alert after restore
    assert "state saved" in store("save")
    log = subprocess.run(["git", "--git-dir", str(origin), "log", "--oneline", "monitor-state"],
                         capture_output=True, text=True, check=True).stdout
    assert len(log.strip().splitlines()) == 2


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_restore_without_saved_state_creates_schema(db_url, tmp_path):
    import psycopg
    if not shutil.which("psql"):
        pytest.skip("needs psql")
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS nyrr_monitor CASCADE")
    origin, work = tmp_path / "o.git", tmp_path / "w"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    shutil.copytree(ROOT / "db", work / "db")
    shutil.copytree(ROOT / "scripts", work / "scripts")
    out = subprocess.run(["bash", "scripts/state_store.sh", "restore"], cwd=work, check=True, capture_output=True,
                         text=True, env={**os.environ, "PGURL": db_url}).stdout
    assert "fresh schema" in out
    with psycopg.connect(db_url) as c:
        assert c.execute("SELECT to_regclass('nyrr_monitor.shifts')").fetchone()[0] is not None
