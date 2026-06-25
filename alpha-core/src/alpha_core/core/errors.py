"""Broker error taxonomy — transient vs terminal (ADR 0004).

The retry layer (``helpers/retry.py``) wraps only ``TransientBrokerError`` and
only idempotent operations. ``TerminalBrokerError`` is never retried. ``AuthError``
is special: credentials/re-auth belong to the human (CLAUDE.md never-do), so it
halts + alerts and is never auto-handled.
"""

from __future__ import annotations


class BrokerError(Exception):
    """Base for all adapter-surfaced errors."""


class TransientBrokerError(BrokerError):
    """Retryable (backoff + jitter, capped attempts)."""


class BrokerTimeout(TransientBrokerError):
    """Request timed out — ambiguous; retry via the idempotent dedup path."""


class BrokerUnavailable(TransientBrokerError):
    """5xx from the venue."""


class BrokerRateLimited(TransientBrokerError):
    """429 — carries the venue's retry-after hint (seconds), when provided."""

    def __init__(self, message: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class BrokerConnectionLost(TransientBrokerError):
    """Websocket/socket drop."""


class TerminalBrokerError(BrokerError):
    """Not retryable — handled per FSM/risk, no backoff."""


class OrderRejected(TerminalBrokerError):
    """The venue rejected the order."""

    def __init__(self, message: str = "", *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class InvalidOrder(TerminalBrokerError):
    """Malformed params / fails venue validation."""


class InsufficientFunds(TerminalBrokerError):
    """Margin / funds shortfall."""


class UnknownOrder(TerminalBrokerError):
    """Cancel/modify of an order the venue doesn't have."""


class AuthError(TerminalBrokerError):
    """401/403 — STOP, alert the human for re-auth. Never auto-handled."""
