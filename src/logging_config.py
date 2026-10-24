"""Structured JSON logging with secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

_PATTERNS = [
    re.compile(r"re_[A-Za-z0-9_]{8,}"),                       # Resend API keys
    re.compile(r"(postgres(?:ql)?://[^:/@\s]+:)[^@\s]+(@)"),   # passwords in DB URLs
    re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"),
]
_STANDARD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {"message", "asctime"}


class Redactor:
    def __init__(self) -> None:
        self._literals: list[str] = []

    def add(self, *values: str | None) -> None:
        for v in values:
            if v and len(v) >= 6 and v not in self._literals:
                self._literals.append(v)

    def __call__(self, text: str) -> str:
        for lit in self._literals:
            text = text.replace(lit, "***")
        for pat in _PATTERNS:
            if pat.groups == 2:
                text = pat.sub(r"\1***\2", text)
            elif pat.groups == 1:
                text = pat.sub(r"\1***", text)
            else:
                text = pat.sub("***", text)
        return text


REDACTOR = Redactor()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return REDACTOR(json.dumps(payload, default=str, ensure_ascii=False))


def configure_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    REDACTOR.add(*(secrets or []))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs full URLs at INFO; keep it quiet
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
