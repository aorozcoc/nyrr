"""End-to-end scenario, configuration (cases 27–28) and workflow validation (case 29)."""

from pathlib import Path

import pytest
import yaml

from src.config import ConfigError, Settings
from src.main import main
from tests.conftest import RECIPIENTS
from tests.test_monitor import BAG, Harness

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ALLOWED_SECRETS = {"GMAIL_APP_PASSWORD", "ALERT_EMAIL_FROM", "ALERT_EMAIL_TO"}


def test_end_to_end_opening_lifecycle(db, site, sender, clock):
    """Baseline → shift opens → both alerted → shift fills → reopens → both alerted again."""
    h = Harness(db, site, sender, clock)
    assert h.run().status == "SUCCESS"
    assert h.alerts() == []
    h.set_bag("available")
    h.run()
    first = h.alerts()
    assert len(first) == 2 and {a["to"] for a in first} == set(RECIPIENTS)
    text = first[0]["text"]
    assert "Bag Check" in text and "register.nyrr.org" in text and "2026 +1 credit: YES" in text
    h.run()  # still open: silence
    h.set_bag("sold-out")
    h.run()
    h.set_bag("available")
    h.run()
    assert len(h.alerts()) == 4
    runs = h.repo._all("SELECT status FROM nyrr_monitor.monitoring_runs ORDER BY started_at")
    assert [r["status"] for r in runs] == ["SUCCESS"] * 5
    t = h.repo._all("""SELECT new_classification FROM nyrr_monitor.availability_transitions
                       WHERE shift_id = %s ORDER BY detected_at""", (BAG,))
    assert [r["new_classification"] for r in t] == ["UNAVAILABLE", "AVAILABLE_VERIFIED", "UNAVAILABLE",
                                                    "AVAILABLE_VERIFIED"]


# ---------------------------------------------------------------- configuration
GOOD_ENV = {
    "DATABASE_URL": "postgresql://u:secretpw@db.example.com:5432/postgres",
    "RESEND_API_KEY": "re_abcdefghijkl",
    "ALERT_EMAIL_FROM": "NYRR Monitor <alerts@plutusiq.example>",
    "ALERT_EMAIL_TO": "recipient.one@example.com, Recipient.Two@example.com",
    "APP_ENV": "production",
}


def test_valid_production_config():
    s = Settings.from_env(GOOD_ENV)
    s.validate_for()
    assert s.alert_email_to == RECIPIENTS
    assert s.nyrr_collection_authorized is False and s.alerts_enabled is False  # safe defaults


@pytest.mark.parametrize("missing", ["DATABASE_URL", "RESEND_API_KEY", "ALERT_EMAIL_FROM", "ALERT_EMAIL_TO"])
def test_27_missing_secret(missing):
    env = {k: v for k, v in GOOD_ENV.items() if k != missing}
    with pytest.raises(ConfigError) as exc:
        Settings.from_env(env).validate_for()
    assert missing in str(exc.value)
    assert "secretpw" not in str(exc.value) and "re_abcdefghijkl" not in str(exc.value)


@pytest.mark.parametrize("key,value,fragment", [
    ("ALERT_EMAIL_FROM", "someone@gmail.com", "verified in Resend"),
    ("ALERT_EMAIL_FROM", "not-an-email", "not a valid email"),
    ("ALERT_EMAIL_TO", "recipient.one@example.com", "exactly the two"),
    ("ALERT_EMAIL_TO", "a@example.com,b@example.com,c@example.com", "exactly the two"),
    ("ALERT_EMAIL_TO", "a@b.com,a@b.com", "duplicates"),
    ("RESEND_API_KEY", "sk_live_123", "Resend key"),
    ("APP_ENV", "staging", "APP_ENV"),
    ("REQUEST_DELAY_SECONDS", "0.5", "REQUEST_DELAY_SECONDS"),
])
def test_28_invalid_configuration(key, value, fragment):
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({**GOOD_ENV, key: value}).validate_for()
    assert fragment in str(exc.value)


def test_main_exits_2_on_missing_secret_without_leaking(monkeypatch, capsys):
    for k in list(GOOD_ENV):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RESEND_API_KEY", "re_shouldnotappear123")
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    assert main(["monitor"]) == 2
    out = capsys.readouterr().out
    assert "configuration_invalid" in out and "re_shouldnotappear123" not in out


def test_main_exits_1_when_database_unreachable(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:topsecretpw@127.0.0.1:1/x?connect_timeout=2")
    monkeypatch.setenv("APP_ENV", "test")
    for k in ("RESEND_API_KEY", "ALERT_EMAIL_FROM", "ALERT_EMAIL_TO", "ALERTS_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    assert main(["monitor"]) == 1
    out = capsys.readouterr().out
    assert "run_crashed" in out and "topsecretpw" not in out


def test_log_redaction():
    from src.logging_config import Redactor
    r = Redactor()
    r.add("hunter2-literal")
    s = r("key re_ABCDEF123456 url postgresql://user:pw123@host/db and hunter2-literal")
    assert "re_ABCDEF123456" not in s and "pw123" not in s and "hunter2-literal" not in s


# ---------------------------------------------------------------- 29: GitHub workflows
def load(name):
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 parses `on:` as boolean True
    return data


def secrets_referenced(text):
    import re
    return set(re.findall(r"secrets\.([A-Z0-9_]+)", text))


def test_29_monitor_workflow():
    wf = load("monitor.yml")
    assert wf["on"]["schedule"] == [{"cron": "*/15 * * * *"}]
    assert "workflow_dispatch" in wf["on"]
    assert wf["permissions"] == {"contents": "read"}
    assert wf["concurrency"] == {"group": "nyrr-monitor-production", "cancel-in-progress": False}
    job = wf["jobs"]["monitor"]
    assert job["timeout-minutes"] == 12 and job["runs-on"] == "ubuntu-latest"
    assert job["permissions"] == {"contents": "write"}  # state branch only
    assert job["services"]["postgres"]["image"] == "postgres:16"
    steps = {s.get("name"): s for s in job["steps"]}
    assert steps["Python setup"]["with"]["python-version"] == "3.12"
    assert "python -m src.main" in steps["Execute monitor"]["run"]
    env = steps["Execute monitor"]["env"]
    assert env["APP_ENV"] == "production" and env["TZ"] == "America/New_York"
    assert env["EMAIL_PROVIDER"] == "gmail" and "localhost" in env["DATABASE_URL"]
    assert "state_store.sh restore" in steps["Restore state"]["run"]
    save = steps["Save state"]
    assert "state_store.sh save" in save["run"] and "always()" in save["if"] and "restore" in save["if"]
    names = [s.get("name") for s in job["steps"]]
    assert names.index("Restore state") < names.index("Execute monitor") < names.index("Save state")
    assert secrets_referenced((WORKFLOWS / "monitor.yml").read_text()) <= ALLOWED_SECRETS
    disable = wf["jobs"]["disable-after-expiry"]
    assert disable["permissions"] == {"actions": "write"} and "expired" in disable["if"]


def test_29_supporting_workflows():
    tests = load("tests.yml")
    assert secrets_referenced((WORKFLOWS / "tests.yml").read_text()) == set()  # PR code never sees secrets
    assert tests["permissions"] == {"contents": "read"}
    health = load("health.yml")
    assert "schedule" in health["on"] and health["permissions"] == {"contents": "read"}
    assert "freshness" in (WORKFLOWS / "health.yml").read_text()
    assert secrets_referenced((WORKFLOWS / "health.yml").read_text()) <= ALLOWED_SECRETS
    assert not (WORKFLOWS / "email-test.yml").exists()  # lite setup: 60-minute test skipped by owner
    for name in ("monitor.yml", "health.yml", "tests.yml"):
        assert "pull_request_target" not in (WORKFLOWS / name).read_text()
