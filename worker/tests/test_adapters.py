"""Adapter-factory tests (M3.1) — the live-gate guard + ccxt construction."""

from __future__ import annotations

import pytest

from alpha_core.adapters.crypto_ccxt import CcxtAdapter
from worker.adapters import build_adapter
from worker.config import EnvConfig, VenueConfig


def _env(**over: object) -> EnvConfig:
    base: dict[str, object] = {
        "env": "paper",
        "mode": "paper",
        "allow_live": False,
        "worker_id": "w1",
        "venue": "v",
        "strategy": "idle",
        "symbols": ["BTC/USDT"],
        "bar_interval_seconds": 60,
        "state_db": "sqlite:///:memory:",
        "heartbeat_path": "var/run/hb",
        "command_poll_seconds": 1.0,
        "reconcile_interval_seconds": 30,
    }
    base.update(over)
    return EnvConfig.model_validate(base)


def _venue(**over: object) -> VenueConfig:
    base: dict[str, object] = {
        "adapter": "ccxt",
        "exchange": "binance",
        "venue": "BINANCE",
        "market_type": "spot",
        "testnet": True,
        "streaming": True,
        "key_env": "TESTKEY",
    }
    base.update(over)
    return VenueConfig.model_validate(base)


def test_build_refuses_live_venue_when_gate_shut() -> None:
    # A non-testnet venue under a paper env must be refused before any key/ccxt touch.
    with pytest.raises(PermissionError, match="live gate is shut"):
        build_adapter(_venue(testnet=False), _env())


def test_build_requires_keys(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("TESTKEY_API_KEY", raising=False)
    monkeypatch.delenv("TESTKEY_API_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="missing TESTKEY_API_KEY"):
        build_adapter(_venue(), _env())


async def test_build_streaming_venue_has_real_watch_methods(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A streaming venue MUST come from ccxt.pro — async_support's watch_* are
    # NotSupported stubs that would kill the tick/order feed. Assert the exchange's
    # watch_trades is a real implementation, not the stub (caught a real regression).
    monkeypatch.setenv("TESTKEY_API_KEY", "k")
    monkeypatch.setenv("TESTKEY_API_SECRET", "s")
    adapter = build_adapter(_venue(streaming=True), _env())
    try:
        assert isinstance(adapter, CcxtAdapter)
        ex = adapter._ex  # the wrapped ccxt exchange
        qualname = type(ex).__module__
        assert "pro" in qualname or "ccxtpro" in qualname  # ccxt.pro, not async_support
    finally:
        await adapter.aclose()


async def test_build_non_streaming_venue_uses_async_support(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TESTKEY_API_KEY", "k")
    monkeypatch.setenv("TESTKEY_API_SECRET", "s")
    adapter = build_adapter(_venue(streaming=False), _env())
    try:
        assert isinstance(adapter, CcxtAdapter)
    finally:
        await adapter.aclose()


async def test_build_live_adapter_when_gate_fully_open(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Exercise the gate-OPEN branch: a live (non-testnet) venue under mode=live +
    # allow_live builds (fake keys, no network). This is the only path past the guard.
    monkeypatch.setenv("LIVEKEY_API_KEY", "k")
    monkeypatch.setenv("LIVEKEY_API_SECRET", "s")
    venue = _venue(testnet=False, streaming=False, key_env="LIVEKEY")
    env = _env(mode="live", allow_live=True)
    adapter = build_adapter(venue, env)
    try:
        assert isinstance(adapter, CcxtAdapter)
    finally:
        await adapter.aclose()


def test_build_unknown_exchange_fails_fast(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TESTKEY_API_KEY", "k")
    monkeypatch.setenv("TESTKEY_API_SECRET", "s")
    with pytest.raises(RuntimeError, match="unknown exchange"):
        build_adapter(_venue(exchange="not_a_real_exchange"), _env())
