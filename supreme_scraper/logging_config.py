"""
logging_config.py — Structured logging setup with credential redaction.

Uses structlog with a JSON renderer for machine-parseable output.

SECURITY: `sensitive_filter` is inserted into the processor chain BEFORE
`JSONRenderer`, ensuring that credential-shaped strings are scrubbed from
every log event before any serialization occurs. This processor is
unconditional — it cannot be disabled via configuration.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import structlog

from supreme_scraper.config import settings

# ------------------------------------------------------------------ #
# Patterns that match common credential shapes                         #
# ------------------------------------------------------------------ #
_SENSITIVE_PATTERNS: list[re.Pattern[str]] = [
    # key=value patterns: token=abc, api_key="xyz", password: secret123
    re.compile(
        r"(?i)(token|api[_-]?key|password|secret|auth)[=:\s\"']+[A-Za-z0-9_\-.]{8,}"
    ),
    # Telegram bot token shape: 1234567890:ABCdef...35chars
    re.compile(r"\b[0-9]{9,}:[A-Za-z0-9_\-]{35}\b"),
]

_REDACT = "[REDACTED]"


def sensitive_filter(
    logger: Any,  # noqa: ARG001
    method: str,  # noqa: ARG001
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """
    structlog processor — redacts credential-shaped substrings from every
    string value in the event dict. Runs before JSONRenderer so no raw
    secret ever reaches log output or stdout.
    """
    for key, value in event_dict.items():
        if isinstance(value, str):
            for pattern in _SENSITIVE_PATTERNS:
                value = pattern.sub(_REDACT, value)
            event_dict[key] = value
    return event_dict


def configure_logging() -> None:
    """
    Call once at application startup (before any logging occurs).

    Processor chain order — the order here is the order events flow through:
      merge_contextvars  — pull in request-scoped fields bound via structlog.contextvars
      add_log_level      — attach the log level string to the event dict
      TimeStamper(iso)   — ISO 8601 timestamp
      sensitive_filter   — SECURITY: redact credentials BEFORE serialization
      StackInfoRenderer  — include stack traces if present
      JSONRenderer       — produce a single-line JSON string per event
    """
    log_level = getattr(logging, settings.LOG_LEVEL, logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            sensitive_filter,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


def get_logger(name: str = __name__) -> Any:
    """Return a structlog logger bound with the given name."""
    return structlog.get_logger(name)
