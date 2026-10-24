"""Logging configuration with secret / personal data redaction."""

from __future__ import annotations

import logging
import re
import sys

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Telegram bot token: 123456789:AA...
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "<bot-token>"),
    # Anthropic keys
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "<anthropic-key>"),
    # Passwords inside DSNs: scheme://user:password@host
    (re.compile(r"(\w+://[^:/\s]+:)[^@\s]+@"), r"\1***@"),
    # Phone numbers
    (re.compile(r"(?<![\d+])(?:\+?\d{1,3}[\s-]?)?\(?\d{2,3}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)|\+?\d{10,15}"), "<phone>"),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        if record.exc_info and not record.exc_text:
            formatter = logging.Formatter()
            record.exc_text = redact(formatter.formatException(record.exc_info))
            record.exc_info = None
        return True


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Third-party loggers can print request URLs that contain the bot token.
    for noisy in ("httpx", "httpx2", "aiohttp.access", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
