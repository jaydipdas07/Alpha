"""Data feed + normalization + CSV loading tests (Phase 3 B3.3)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.interfaces import BrokerAdapter, BrokerOrderEvent
from alpha_core.core.models import Order, Position, Tick
from alpha_core.data.feed import AdapterFeed, ReplayFeed
from alpha_core.data.historical import load_bars_csv, load_ticks_csv
from alpha_core.data.normalize import bar_from_row, tick_from_row

T0 = datetime(2026, 6, 15, 4, 0, tzinfo=UTC)


def _tick(symbol: str, price: str, ts: datetime = T0) -> Tick:
    return Tick(
        symbol=symbol,
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        ts=ts,
        last_price=Decimal(price),
    )


# --- normalization -------------------------------------------------------------


def test_tick_from_row_decimal_and_utc() -> None:
    row = {
        "symbol": "NSE:RELIANCE",
        "venue": "NSE",
        "asset_class": "EQUITY",
        "ts": "2026-06-15T09:15:00+05:30",
        "last_price": "2500.5",
        "bid": "",
        "ask": "",
        "volume": "100",
    }
    t = tick_from_row(row)
    assert t.last_price == Decimal("2500.5")
    assert isinstance(t.last_price, Decimal)
    assert t.bid is None
    assert t.ts.tzinfo is UTC  # normalized from IST


def test_bar_from_row() -> None:
    row = {
        "symbol": "NSE:RELIANCE",
        "venue": "NSE",
        "asset_class": "EQUITY",
        "start": "2026-06-15T03:45:00+00:00",
        "interval_seconds": "60",
        "open": "100",
        "high": "101",
        "low": "99",
        "close": "100.5",
        "volume": "5000",
    }
    b = bar_from_row(row)
    assert b.interval == timedelta(minutes=1)
    assert b.high == Decimal("101")


# --- replay feed ---------------------------------------------------------------


async def _collect(aiter: object) -> list[object]:
    return [x async for x in aiter]  # type: ignore[attr-defined]


async def test_replay_feed_filters_by_symbol() -> None:
    feed = ReplayFeed(
        ticks=[
            _tick("NSE:RELIANCE", "100"),
            _tick("NSE:TCS", "3000"),
            _tick("NSE:RELIANCE", "101"),
        ]
    )
    out = [t async for t in feed.stream_ticks(["NSE:RELIANCE"])]
    assert [t.last_price for t in out] == [Decimal("100"), Decimal("101")]


async def test_replay_feed_preserves_order() -> None:
    ticks = [_tick("NSE:RELIANCE", str(p), T0 + timedelta(seconds=p)) for p in range(1, 6)]
    feed = ReplayFeed(ticks=ticks)
    out = [t async for t in feed.stream_ticks(["NSE:RELIANCE"])]
    assert [t.ts for t in out] == [t.ts for t in ticks]


async def test_replay_feed_empty() -> None:
    feed = ReplayFeed()
    assert [t async for t in feed.stream_ticks(["X"])] == []


# --- adapter feed (paper-crypto: live ticks back the paper broker) -------------


class _TickOnlyAdapter(BrokerAdapter):
    """Minimal adapter that only streams ticks — enough to back an AdapterFeed."""

    def __init__(self, ticks: list[Tick]) -> None:
        self._ticks = ticks

    async def subscribe_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        wanted = set(symbols)
        for t in self._ticks:
            if t.symbol in wanted:
                yield t

    async def place_order(self, order: Order) -> str:
        raise NotImplementedError

    async def cancel(self, client_order_id: str) -> None:
        raise NotImplementedError

    async def modify(self, client_order_id: str, **kwargs: object) -> None:
        raise NotImplementedError

    async def get_orders(self) -> list[Order]:
        return []

    async def get_positions(self) -> list[Position]:
        return []

    async def find_order_id(self, client_order_id: str) -> str | None:
        return None

    def order_events(self) -> AsyncIterator[BrokerOrderEvent]:
        raise NotImplementedError


def _crypto_tick(symbol: str, price: str) -> Tick:
    return Tick(
        symbol=symbol,
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        ts=T0,
        last_price=Decimal(price),
        volume=Decimal("0.5"),
    )


async def test_adapter_feed_streams_ticks_from_the_adapter() -> None:
    # AdapterFeed lets the paper broker price fills against a real venue's live
    # ticks (paper-crypto) — it delegates stream_ticks to the wrapped adapter.
    feed = AdapterFeed(
        _TickOnlyAdapter([_crypto_tick("BTC/USDT", "65000"), _crypto_tick("ETH/USDT", "3000")])
    )
    out = [t async for t in feed.stream_ticks(["BTC/USDT"])]
    assert [t.last_price for t in out] == [Decimal("65000")]


def test_adapter_feed_does_not_stream_bars() -> None:
    # Bars are built from ticks by BarBuilder, not streamed by the adapter feed.
    with pytest.raises(NotImplementedError):
        AdapterFeed(_TickOnlyAdapter([])).stream_bars(["BTC/USDT"])


# --- CSV round trip ------------------------------------------------------------


def test_load_ticks_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "ticks.csv"
    csv_path.write_text(
        "symbol,venue,asset_class,ts,last_price,volume\n"
        "NSE:RELIANCE,NSE,EQUITY,2026-06-15T09:15:00+05:30,2500.5,100\n"
        "NSE:RELIANCE,NSE,EQUITY,2026-06-15T09:15:01+05:30,2500.6,150\n"
    )
    ticks = load_ticks_csv(csv_path)
    assert len(ticks) == 2
    assert ticks[1].last_price == Decimal("2500.6")


def test_load_bars_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "bars.csv"
    csv_path.write_text(
        "symbol,venue,asset_class,start,interval_seconds,open,high,low,close,volume\n"
        "NSE:RELIANCE,NSE,EQUITY,2026-06-15T03:45:00+00:00,60,100,101,99,100.5,5000\n"
    )
    bars = load_bars_csv(csv_path)
    assert len(bars) == 1
    assert bars[0].close == Decimal("100.5")
