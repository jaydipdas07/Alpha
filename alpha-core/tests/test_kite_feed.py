"""KiteTickerFeed tests (M4.5) — thread-bridged normalization of Kite ticker packets.

A fake ticker drives the protocol exactly as ``kiteconnect.KiteTicker`` does: callbacks
assigned, ``connect(threaded=True)``, ticks delivered from a REAL background thread (the
bridge under test), subscriptions re-issued on every connect, ``on_noreconnect`` as the
terminal error. Money is Decimal from the SDK's floats; naive-IST stamps become UTC.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Tick
from alpha_core.data.kite_feed import KiteTickerFeed

TOKENS = {"NSE:RELIANCE": 738561, "NSE:INFY": 408065}


def _raw(
    token: int = 738561,
    last: float | None = 2871.4,
    *,
    depth: bool = True,
    ts: datetime | None = datetime(2026, 7, 3, 10, 0, 0),  # naive IST
) -> dict[str, Any]:
    raw: dict[str, Any] = {"instrument_token": token, "volume_traded": 123456}
    if last is not None:
        raw["last_price"] = last
    if depth:
        raw["depth"] = {
            "buy": [{"price": 2871.35, "quantity": 50}],
            "sell": [{"price": 2871.45, "quantity": 75}],
        }
    if ts is not None:
        raw["exchange_timestamp"] = ts
    return raw


class _FakeTicker:
    """The KiteTickerLike surface, driven like the real SDK (threaded delivery)."""

    MODE_FULL = "full"

    def __init__(self, batches: list[list[dict[str, Any]]], *, then_noreconnect: bool = False):
        self._batches = batches
        self._then_noreconnect = then_noreconnect
        self.on_ticks: Any = None
        self.on_connect: Any = None
        self.on_error: Any = None
        self.on_noreconnect: Any = None
        self.subscribed: list[list[int]] = []
        self.modes: list[tuple[str, list[int]]] = []
        self.closed = False
        self._thread: threading.Thread | None = None

    def connect(self, threaded: bool = False) -> None:
        assert threaded, "the feed must never run the ws reactor on the event loop"
        self.on_connect(self, None)  # Kite fires on_connect from the ws thread

        def _deliver() -> None:
            for batch in self._batches:
                self.on_ticks(self, batch)
            if self._then_noreconnect:
                self.on_noreconnect(self)

        self._thread = threading.Thread(target=_deliver)
        self._thread.start()

    def subscribe(self, instrument_tokens: list[int]) -> None:
        self.subscribed.append(list(instrument_tokens))

    def set_mode(self, mode: str, instrument_tokens: list[int]) -> None:
        self.modes.append((mode, list(instrument_tokens)))

    def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.closed = True
        if self._thread is not None:
            self._thread.join(timeout=5)


def _feed(ticker: _FakeTicker) -> KiteTickerFeed:
    return KiteTickerFeed(ticker, token_by_symbol=TOKENS)


async def _collect(feed: KiteTickerFeed, symbols: list[str], n: int) -> list[Tick]:
    out: list[Tick] = []
    agen = cast("AsyncGenerator[Tick, None]", feed.stream_ticks(symbols))
    try:
        async with asyncio.timeout(5):
            async for tick in agen:
                out.append(tick)
                if len(out) == n:
                    break
    finally:
        await agen.aclose()
    return out


async def test_ticks_bridge_from_the_ws_thread_normalized() -> None:
    ticker = _FakeTicker([[_raw()]])
    (tick,) = await _collect(_feed(ticker), ["NSE:RELIANCE"], 1)
    assert tick.symbol == "NSE:RELIANCE"
    assert tick.venue is Venue.NSE
    assert tick.asset_class is AssetClass.EQUITY
    assert tick.last_price == Decimal("2871.4")  # Decimal(str(float)) — no float artifacts
    assert tick.bid == Decimal("2871.35")
    assert tick.ask == Decimal("2871.45")
    assert tick.volume == Decimal("123456")
    # naive 10:00 IST -> 04:30 UTC, tz-aware
    assert tick.ts == datetime(2026, 7, 3, 4, 30, 0, tzinfo=UTC)


async def test_connect_subscribes_full_mode_for_exactly_the_asked_symbols() -> None:
    ticker = _FakeTicker([[_raw()]])
    await _collect(_feed(ticker), ["NSE:RELIANCE"], 1)
    assert ticker.subscribed == [[738561]]  # only the streamed symbol, not the whole map
    assert ticker.modes == [("full", [738561])]
    assert ticker.closed  # released on aclose


async def test_unusable_and_foreign_ticks_are_dropped() -> None:
    batches = [
        [
            _raw(last=0.0, depth=False),  # zero price, no depth -> nothing to price with
            _raw(token=999999),  # not a subscribed token (defensive)
            _raw(token=408065),  # the good one
        ]
    ]
    (tick,) = await _collect(_feed(_FakeTicker(batches)), ["NSE:INFY", "NSE:RELIANCE"], 1)
    assert tick.symbol == "NSE:INFY"


async def test_missing_stamp_degrades_to_arrival_time() -> None:
    before = datetime.now(UTC)
    (tick,) = await _collect(_feed(_FakeTicker([[_raw(ts=None)]])), ["NSE:RELIANCE"], 1)
    assert before <= tick.ts <= datetime.now(UTC)


async def test_noreconnect_ends_the_stream_after_delivered_ticks() -> None:
    ticker = _FakeTicker([[_raw()]], then_noreconnect=True)
    feed = _feed(ticker)
    got: list[Tick] = []
    with pytest.raises(ConnectionError, match="gave up reconnecting"):
        async with asyncio.timeout(5):
            async for tick in feed.stream_ticks(["NSE:RELIANCE"]):
                got.append(tick)
    assert len(got) == 1  # the tick before the terminal close was not lost
    assert ticker.closed


async def test_unknown_symbol_fails_fast() -> None:
    feed = _feed(_FakeTicker([]))
    with pytest.raises(KeyError, match="NSE:SBIN"):
        await _collect(feed, ["NSE:SBIN"], 1)


def test_stream_bars_is_not_supported() -> None:
    with pytest.raises(NotImplementedError):
        _feed(_FakeTicker([])).stream_bars(["NSE:RELIANCE"])
