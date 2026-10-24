"""CLI wiring (`python -m src.main ...`) and operator scripts, with a fake email provider."""

import importlib.util
from pathlib import Path

import pytest

import src.monitor
from src.main import main
from tests.conftest import RECIPIENTS

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env(monkeypatch, db_url, db, sender, tmp_path):
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("RESEND_API_KEY", "re_test_cli_123456")
    monkeypatch.setenv("ALERT_EMAIL_FROM", "NYRR Monitor <alerts@example.org>")
    monkeypatch.setenv("ALERT_EMAIL_TO", ",".join(RECIPIENTS))
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out.txt"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setattr(src.monitor, "_default_sender", lambda s: sender)
    return tmp_path


def test_monitor_cli_not_authorized_makes_no_requests(env, monkeypatch, repo):
    monkeypatch.delenv("NYRR_COLLECTION_AUTHORIZED", raising=False)
    assert main([]) == 0
    assert "status=NOT_AUTHORIZED" in (env / "out.txt").read_text()
    assert "expired=false" in (env / "out.txt").read_text()
    row = repo._one("SELECT status, error_summary FROM nyrr_monitor.monitoring_runs")
    assert row["status"] == "NOT_AUTHORIZED" and "Gate 1" in row["error_summary"]


def test_health_cli(env, repo, sender):
    assert main(["health"]) == 0
    assert "health=INACTIVE" in (env / "out.txt").read_text()
    assert sender.sent == []


def test_health_cli_database_unreachable_sends_alert(env, monkeypatch, sender):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:pw@127.0.0.1:1/x?connect_timeout=2")
    assert main(["health"]) == 1
    assert len(sender.sent) == 2 and sender.sent[0]["subject"] == "NYRR MONITOR — SYSTEM HEALTH ALERT"


def test_email_test_cli_start_tick_report(env, sender):
    assert main(["email-test", "start"]) == 2  # --authorized-by is mandatory
    assert main(["email-test", "start", "--authorized-by", "owner"]) == 0
    assert len(sender.sent) == 2
    assert main(["email-test", "tick"]) == 0  # not due yet
    assert "finished=false" in (env / "out.txt").read_text()
    out = env / "EMAIL_TEST_RESULTS.md"
    assert main(["email-test", "report", "--output", str(out)]) == 0
    assert "Actual executions: 1" in out.read_text()
    assert main(["email-test", "start", "--authorized-by", "owner"]) == 1  # refused: already active


def test_script_validate_config(env, capsys):
    assert load_script("validate_config").main([]) == 0
    text = capsys.readouterr().out
    assert "re_test_cli_123456" not in text and "OK" in text


def test_script_test_email_requires_confirmation(env, sender, capsys):
    mod = load_script("test_email")
    assert mod.main([], sender_factory=lambda: sender) == 0
    assert sender.sent == []  # dry run
    assert mod.main(["--confirm-send"], sender_factory=lambda: sender) == 0
    assert [s["subject"] for s in sender.sent] == ["NYRR MONITOR — TEST NOTIFICATION"] * 2
    assert "No actual volunteer opening has been detected." in sender.sent[0]["text"]
    assert "Accepted ≠ delivered" in capsys.readouterr().out


def test_script_check_health(env, repo, sender, capsys):
    assert load_script("check_health").main([]) == 0
    assert "INACTIVE" in capsys.readouterr().out
    assert load_script("check_health").main(["--notify"], sender=sender) == 0
