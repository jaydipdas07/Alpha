"""Alerting (ADR 0006/0012 — kill switch, failures, heartbeat).

A minimal ``Notifier`` protocol with two implementations:
- ``LoggingNotifier`` — always available, writes a structured ``alert`` log; the
  default used in dev/paper and tests (no secrets, no network).
- ``TelegramNotifier`` — reads its bot token + chat id from the **environment**
  only (never code/config), per ADR 0011. The actual HTTP send is wired when the
  observability stack is deployed (Phase 9); construction validates presence of
  credentials so misconfiguration fails fast.

Severity is a small enum so callers don't pass free-form strings.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Protocol, runtime_checkable

from alpha_core.observability.logging import get_logger


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@runtime_checkable
class Notifier(Protocol):
    """Sends an alert. Implementations must never raise on a transient failure
    in a way that crashes the trading loop (best-effort delivery)."""

    def send(self, message: str, *, severity: Severity = Severity.INFO) -> None: ...


class LoggingNotifier:
    """Writes alerts to the structured log. Default for dev/paper/tests."""

    def __init__(self) -> None:
        self._log = get_logger("notify")

    def send(self, message: str, *, severity: Severity = Severity.INFO) -> None:
        self._log.warning("alert", message=message, severity=severity.value)


class TelegramNotifier:
    """Telegram alerts; credentials come from the environment only.

    Construction fails fast if the token/chat id are absent. Delivery is
    best-effort — a failed send is logged, never raised, so alerting can't crash
    the trading loop. The token is never logged.
    """

    TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
    CHAT_ENV = "TELEGRAM_CHAT_ID"

    def __init__(self) -> None:
        token = os.environ.get(self.TOKEN_ENV)
        chat_id = os.environ.get(self.CHAT_ENV)
        if not token or not chat_id:
            raise ValueError(
                f"TelegramNotifier requires {self.TOKEN_ENV} and {self.CHAT_ENV} in the environment"
            )
        self._token = token
        self._chat_id = chat_id
        self._log = get_logger("notify")

    def send(self, message: str, *, severity: Severity = Severity.INFO) -> None:
        from alpha_core.helpers.telegram import send_message

        ok = send_message(self._token, self._chat_id, f"[{severity.value}] {message}")
        if not ok:
            self._log.warning("alert_send_failed", severity=severity.value, channel="telegram")


def notifier_from_env() -> Notifier:
    """Pick the alert channel from the environment (P13.11).

    Returns a ``TelegramNotifier`` when both ``TELEGRAM_BOT_TOKEN`` and
    ``TELEGRAM_CHAT_ID`` are set, else the always-available ``LoggingNotifier``.
    Used by the live runner so kill-switch / heartbeat / failure alerts reach the
    operator's phone when configured.
    """
    if os.environ.get(TelegramNotifier.TOKEN_ENV) and os.environ.get(TelegramNotifier.CHAT_ENV):
        return TelegramNotifier()
    return LoggingNotifier()
