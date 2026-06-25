"""Observability tests: logging redaction, metrics, heartbeat, notify."""

from __future__ import annotations

import pytest

from alpha_core.observability import metrics
from alpha_core.observability.heartbeat import Heartbeat
from alpha_core.observability.logging import _redact
from alpha_core.observability.notify import (
    LoggingNotifier,
    Notifier,
    Severity,
    TelegramNotifier,
)

# --- logging redaction ---------------------------------------------------------


def test_redact_masks_secret_keys() -> None:
    out = _redact(None, "info", {"api_key": "abc", "msg": "hi", "auth_token": "xyz"})
    assert out["api_key"] == "***REDACTED***"
    assert out["auth_token"] == "***REDACTED***"
    assert out["msg"] == "hi"


def test_redact_leaves_clean_keys() -> None:
    out = _redact(None, "info", {"symbol": "NSE:RELIANCE", "qty": 10})
    assert out == {"symbol": "NSE:RELIANCE", "qty": 10}


# --- metrics -------------------------------------------------------------------


def test_metrics_increment() -> None:
    before = metrics.registry.get_sample_value(
        "vega_orders_placed_total", {"venue": "PAPER", "strategy": "s1"}
    )
    metrics.orders_placed.labels(venue="PAPER", strategy="s1").inc()
    after = metrics.registry.get_sample_value(
        "vega_orders_placed_total", {"venue": "PAPER", "strategy": "s1"}
    )
    assert (after or 0) == (before or 0) + 1


def test_gauge_set() -> None:
    metrics.open_positions.set(3)
    assert metrics.registry.get_sample_value("vega_open_positions") == 3


# --- heartbeat -----------------------------------------------------------------


def test_heartbeat_dead_before_first_beat() -> None:
    hb = Heartbeat(10.0, now=lambda: 0.0)
    assert hb.is_alive() is False


def test_heartbeat_alive_then_stale() -> None:
    clock = {"t": 0.0}
    hb = Heartbeat(10.0, miss_factor=3.0, now=lambda: clock["t"])
    hb.beat()
    clock["t"] = 25.0
    assert hb.is_alive() is True  # within 30s budget
    clock["t"] = 31.0
    assert hb.is_alive() is False  # beyond 3 * 10s


def test_heartbeat_rejects_bad_args() -> None:
    with pytest.raises(ValueError):
        Heartbeat(0)
    with pytest.raises(ValueError):
        Heartbeat(10, miss_factor=0.5)


# --- notify --------------------------------------------------------------------


def test_logging_notifier_is_notifier() -> None:
    n = LoggingNotifier()
    assert isinstance(n, Notifier)
    n.send("kill switch tripped", severity=Severity.CRITICAL)  # must not raise


def test_telegram_requires_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(ValueError):
        TelegramNotifier()


def test_telegram_constructs_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    n = TelegramNotifier()
    assert isinstance(n, Notifier)
    n.send("hi")  # must not raise


# --- notifier selection (P13.11) -----------------------------------------------


def test_notifier_from_env_logging_without_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    from alpha_core.observability.notify import LoggingNotifier, notifier_from_env

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert isinstance(notifier_from_env(), LoggingNotifier)


def test_notifier_from_env_telegram_with_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    from alpha_core.observability.notify import TelegramNotifier, notifier_from_env

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    assert isinstance(notifier_from_env(), TelegramNotifier)
