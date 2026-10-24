"""Configuration from environment variables. Secret values are never logged or echoed."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import date
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, SecretStr

EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")
TRUE_VALUES = {"1", "true", "yes", "on"}

# The specification requires exactly two alert recipients. Their addresses live only in the
# ALERT_EMAIL_TO secret, never in the repository.
REQUIRED_RECIPIENT_COUNT = 2


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid. Message never contains secret values."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Invalid configuration: " + "; ".join(problems))


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    database_url: SecretStr | None = None
    email_provider: str = "resend"            # resend | gmail
    resend_api_key: SecretStr | None = None
    gmail_app_password: SecretStr | None = None
    alert_email_from: str | None = None
    alert_email_to: tuple[str, ...] = ()
    app_env: str = "development"

    nyrr_base_url: str = "https://prod.nyrr.org"
    nyrr_calendar_path: str = "/get-involved-volunteer-opportunities"
    nyrr_collection_authorized: bool = False
    nyrr_contact_email: str | None = None
    registration_hosts: tuple[str, ...] = ("register.nyrr.org",)

    alerts_enabled: bool = False
    monitoring_year: int = 2026
    monitoring_end_date: date = date(2026, 12, 31)
    timezone: str = "America/New_York"

    request_delay_seconds: float = 2.0
    max_requests_per_run: int = 30
    full_sweep_interval_minutes: int = 60
    evidence_max_age_minutes: int = 20
    lock_ttl_seconds: int = 720
    retry_delays_seconds: tuple[int, ...] = (60, 300, 900)
    max_send_attempts: int = 3
    dispatch_wait_budget_seconds: int = 420

    heartbeat_url: SecretStr | None = None
    run_trigger: str = "local"

    @property
    def recipient_keys(self) -> tuple[str, ...]:
        """Pseudonymous keys stored in the database instead of addresses."""
        return tuple(f"recipient-{i + 1}" for i in range(len(self.alert_email_to)))

    def resolve_recipient(self, key: str) -> str | None:
        mapping = dict(zip(self.recipient_keys, self.alert_email_to, strict=True))
        if key in mapping:
            return mapping[key]
        return key if "@" in key else None  # rows written before pseudonymisation

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def user_agent(self) -> str:
        contact = f"; contact: {self.nyrr_contact_email}" if self.nyrr_contact_email else ""
        return f"NYRR-9plus1-personal-alert/1.0 (personal volunteer-opening notifier{contact})"

    # ------------------------------------------------------------------ factory
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        def get(name: str) -> str | None:
            value = env.get(name)
            if value is None:
                return None
            value = value.strip()
            return value or None

        def flag(name: str, default: bool = False) -> bool:
            value = get(name)
            return default if value is None else value.lower() in TRUE_VALUES

        recipients = tuple(
            r.strip().lower() for r in (get("ALERT_EMAIL_TO") or "").split(",") if r.strip()
        )
        kwargs: dict = {
            "database_url": SecretStr(v) if (v := get("DATABASE_URL")) else None,
            "resend_api_key": SecretStr(v) if (v := get("RESEND_API_KEY")) else None,
            "gmail_app_password": SecretStr(v) if (v := get("GMAIL_APP_PASSWORD")) else None,
            "email_provider": (get("EMAIL_PROVIDER") or "resend").lower(),
            "alert_email_from": get("ALERT_EMAIL_FROM"),
            "alert_email_to": recipients,
            "app_env": get("APP_ENV") or "development",
            "nyrr_collection_authorized": flag("NYRR_COLLECTION_AUTHORIZED"),
            "nyrr_contact_email": get("NYRR_CONTACT_EMAIL"),
            "alerts_enabled": flag("ALERTS_ENABLED"),
            "heartbeat_url": SecretStr(v) if (v := get("HEARTBEAT_URL")) else None,
            "run_trigger": get("MONITOR_TRIGGER") or "local",
        }
        if v := get("NYRR_BASE_URL"):
            kwargs["nyrr_base_url"] = v.rstrip("/")
        if v := get("REQUEST_DELAY_SECONDS"):
            kwargs["request_delay_seconds"] = float(v)
        if v := get("MAX_REQUESTS_PER_RUN"):
            kwargs["max_requests_per_run"] = int(v)
        return cls(**kwargs)

    # ------------------------------------------------------------------ validation
    def problems(self, *, need_db: bool = True, need_email: bool = True) -> list[str]:
        problems: list[str] = []
        if need_db and self.database_url is None:
            problems.append("DATABASE_URL is missing")
        if need_email:
            gmail = self.email_provider == "gmail"
            if self.email_provider not in ("resend", "gmail"):
                problems.append("EMAIL_PROVIDER must be resend or gmail")
            elif gmail:
                if self.gmail_app_password is None:
                    problems.append("GMAIL_APP_PASSWORD is missing")
                elif len(self.gmail_app_password.get_secret_value().replace(" ", "")) != 16:
                    problems.append("GMAIL_APP_PASSWORD should be the 16-character Google app password")
            elif self.resend_api_key is None:
                problems.append("RESEND_API_KEY is missing")
            elif not self.resend_api_key.get_secret_value().startswith("re_"):
                problems.append("RESEND_API_KEY does not look like a Resend key (expected prefix re_)")
            if not self.alert_email_from:
                problems.append("ALERT_EMAIL_FROM is missing")
            elif not EMAIL_RE.match(_bare_address(self.alert_email_from)):
                problems.append("ALERT_EMAIL_FROM is not a valid email address")
            elif gmail and not _bare_address(self.alert_email_from).lower().endswith(("@gmail.com", "@googlemail.com")):
                problems.append("with EMAIL_PROVIDER=gmail, ALERT_EMAIL_FROM must be the Gmail address")
            elif not gmail and _bare_address(self.alert_email_from).lower().endswith(("@gmail.com", "@googlemail.com")):
                problems.append("ALERT_EMAIL_FROM must be on a domain verified in Resend, not gmail.com")
            if not self.alert_email_to:
                problems.append("ALERT_EMAIL_TO is missing")
            else:
                bad = [r for r in self.alert_email_to if not EMAIL_RE.match(r)]
                if bad:
                    problems.append(f"ALERT_EMAIL_TO has {len(bad)} invalid address(es)")
                if len(set(self.alert_email_to)) != len(self.alert_email_to):
                    problems.append("ALERT_EMAIL_TO contains duplicates")
                if self.app_env == "production" and len(set(self.alert_email_to)) != REQUIRED_RECIPIENT_COUNT:
                    problems.append("ALERT_EMAIL_TO must contain exactly the two configured recipients")
        if self.app_env not in {"development", "test", "production"}:
            problems.append("APP_ENV must be development, test or production")
        if self.request_delay_seconds < 2.0 and self.app_env == "production":
            problems.append("REQUEST_DELAY_SECONDS below 2.0 is not allowed in production")
        if not 1 <= self.max_requests_per_run <= 60:
            problems.append("MAX_REQUESTS_PER_RUN must be between 1 and 60")
        return problems

    def validate_for(self, *, need_db: bool = True, need_email: bool = True) -> None:
        problems = self.problems(need_db=need_db, need_email=need_email)
        if problems:
            raise ConfigError(problems)

    def secret_values(self) -> list[str]:
        out = []
        for s in (self.database_url, self.resend_api_key, self.gmail_app_password, self.heartbeat_url):
            if s is not None:
                out.append(s.get_secret_value())
        return out


def _bare_address(value: str) -> str:
    """'Name <a@b.c>' -> 'a@b.c'."""
    m = re.search(r"<([^>]+)>", value)
    return (m.group(1) if m else value).strip()
