"""Structured logging with secret redaction (ADR 0011 — secrets never logged).

``structlog`` renders machine-parseable JSON. A redaction processor masks any
event field whose key looks secret, so a stray credential never reaches the logs.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any

import structlog

_SECRET_MARKERS = (
    "api_key",
    "api_secret",
    "secret",
    "token",
    "password",
    "private_key",
    "authorization",
)
_REDACTED = "***REDACTED***"


def _redact(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Mask secret-looking keys anywhere in the event dict."""
    for key in list(event_dict):
        if isinstance(key, str) and any(m in key.lower() for m in _SECRET_MARKERS):
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    """Configure ``structlog`` for JSON output with redaction. Idempotent."""
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            _redact,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structured logger."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
