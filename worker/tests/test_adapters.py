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


async def test_testnet_url_override_applied(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A venue whose sandbox isn't ccxt's default testnet (Delta India demo) overrides
    # the ccxt api url so requests hit the right endpoint.
    monkeypatch.setenv("TESTKEY_API_KEY", "k")
    monkeypatch.setenv("TESTKEY_API_SECRET", "s")
    url = "https://cdn-ind.testnet.deltaex.org"
    venue = _venue(exchange="delta", market_type="swap", streaming=False, testnet_url=url)
    adapter = build_adapter(venue, _env())
    try:
        assert adapter._ex.urls["api"] == {"public": url, "private": url}  # type: ignore[attr-defined]
    finally:
        await adapter.aclose()


# --- build_kite_ticker_feed (M4.5 — the data-only Kite factory) -------------------


class _FakeKiteConnect:
    """Stands in for kiteconnect.KiteConnect: serves the instrument master."""

    dumps = 0

    def __init__(self, api_key: str, access_token: str) -> None:
        self.api_key = api_key
        self.access_token = access_token

    def instruments(self, exchange: str) -> list[dict[str, object]]:
        assert exchange == "NSE"
        type(self).dumps += 1
        return [
            {
                "instrument_token": 738561,
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "segment": "NSE",
                "instrument_type": "EQ",
                "lot_size": 1,
                "tick_size": 0.05,
                "expiry": "",
            },
            {
                "instrument_token": 408065,
                "tradingsymbol": "INFY",
                "exchange": "NSE",
                "segment": "NSE",
                "instrument_type": "EQ",
                "lot_size": 1,
                "tick_size": 0.05,
                "expiry": "",
            },
        ]


class _FakeKiteTicker:
    def __init__(self, api_key: str, access_token: str) -> None:
        self.api_key = api_key
        self.access_token = access_token


def _kite_env(tmp_path: object, **over: object) -> EnvConfig:
    base: dict[str, object] = {
        "venue": "kite-nse",
        "execution": "paper",
        "symbols": ["NSE:RELIANCE"],
        "kite_instruments_cache": f"{tmp_path}/kite_instruments.json",
    }
    base.update(over)
    return _env(**base)


def _kite_venue() -> VenueConfig:
    return _venue(
        adapter="kite", exchange="kite", venue="NSE", market_type="equity", key_env="KITE"
    )


@pytest.fixture
def _kite_sdk(monkeypatch):  # type: ignore[no-untyped-def]
    """Install a fake `kiteconnect` module (the factory lazy-imports it)."""
    import sys
    import types

    mod = types.ModuleType("kiteconnect")
    mod.KiteConnect = _FakeKiteConnect  # type: ignore[attr-defined]
    mod.KiteTicker = _FakeKiteTicker  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kiteconnect", mod)
    monkeypatch.setenv("KITE_API_KEY", "k-api")
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "k-tok")
    monkeypatch.delenv("KITE_ACCESS_TOKEN_AT", raising=False)
    _FakeKiteConnect.dumps = 0
    return mod


def test_kite_feed_factory_fetches_caches_and_maps_tokens(_kite_sdk, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from pathlib import Path

    from worker.adapters import build_kite_ticker_feed

    env = _kite_env(tmp_path)
    feed = build_kite_ticker_feed(_kite_venue(), env)
    assert feed._token_by_symbol == {"NSE:RELIANCE": 738561}  # filtered to env.symbols
    assert feed._ticker.api_key == "k-api"  # type: ignore[attr-defined]
    assert Path(env.kite_instruments_cache).is_file()  # dump cached for the next boot
    # Second build: served from the cache — no second network dump.
    build_kite_ticker_feed(_kite_venue(), env)
    assert _FakeKiteConnect.dumps == 1


def test_kite_feed_factory_requires_the_daily_token(_kite_sdk, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from worker.adapters import build_kite_ticker_feed

    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="KITE_ACCESS_TOKEN"):
        build_kite_ticker_feed(_kite_venue(), _kite_env(tmp_path))


def test_kite_feed_factory_fails_fast_on_an_unknown_symbol(_kite_sdk, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from worker.adapters import build_kite_ticker_feed

    with pytest.raises(RuntimeError, match="NSE:NOSUCH"):
        build_kite_ticker_feed(_kite_venue(), _kite_env(tmp_path, symbols=["NSE:NOSUCH"]))
