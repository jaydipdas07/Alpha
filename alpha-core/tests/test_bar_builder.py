"""BarBuilder tests — tick -> closed interval bars (M3.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Tick
from alpha_core.data.bar_builder import BarBuilder

T0 = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)


def _tick(
    *,
    sec: float = 0,
    price: str | None = "100",
    volume: str | None = "1",
    bid: str | None = None,
    ask: str | None = None,
    symbol: str = "BTC/USDT",
) -> Tick:
    return Tick(
        symbol=symbol,
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        ts=T0 + timedelta(seconds=sec),
        last_price=Decimal(price) if price is not None else None,
        volume=Decimal(volume) if volume is not None else None,
        bid=Decimal(bid) if bid is not None else None,
        ask=Decimal(ask) if ask is not None else None,
    )


def test_rejects_nonpositive_interval() -> None:
    with pytest.raises(ValueError, match="positive"):
        BarBuilder(0)


def test_accumulates_within_a_bucket_then_flush() -> None:
    bb = BarBuilder(60)
    assert bb.add(_tick(sec=1, price="100", volume="1")) is None
    assert bb.add(_tick(sec=20, price="105", volume="2")) is None  # high
    assert bb.add(_tick(sec=40, price="99", volume="0.5")) is None  # low + close
    bar = bb.flush("BTC/USDT")
    assert bar is not None
    assert bar.start == T0  # floored to the minute
    assert (bar.open, bar.high, bar.low, bar.close) == (
        Decimal("100"),
        Decimal("105"),
        Decimal("99"),
        Decimal("99"),
    )
    assert bar.volume == Decimal("3.5")
    assert bar.interval == timedelta(seconds=60)
    assert bb.flush("BTC/USDT") is None  # nothing left


def test_crossing_into_new_bucket_emits_prior_bar() -> None:
    bb = BarBuilder(60)
    bb.add(_tick(sec=10, price="100", volume="1"))
    bb.add(_tick(sec=50, price="110", volume="1"))
    emitted = bb.add(_tick(sec=65, price="111", volume="1"))  # next minute -> closes the first
    assert emitted is not None
    assert emitted.start == T0
    assert (emitted.open, emitted.high, emitted.close) == (
        Decimal("100"),
        Decimal("110"),
        Decimal("110"),
    )
    assert emitted.volume == Decimal("2")  # the rolling tick's volume is NOT folded in
    # the new bar is forming; flush it
    nxt = bb.flush("BTC/USDT")
    assert nxt is not None and nxt.start == T0 + timedelta(seconds=60)
    assert nxt.open == Decimal("111") and nxt.volume == Decimal("1")  # the rolling tick opened it


def test_tick_exactly_on_boundary_opens_the_new_bucket() -> None:
    bb = BarBuilder(60)
    bb.add(_tick(sec=30, price="100"))
    emitted = bb.add(_tick(sec=60, price="120"))  # exactly the boundary -> new bucket
    assert emitted is not None and emitted.start == T0 and emitted.close == Decimal("100")
    nxt = bb.flush("BTC/USDT")
    assert nxt is not None and nxt.start == T0 + timedelta(seconds=60)


def test_bucket_alignment_floors_to_interval() -> None:
    bb = BarBuilder(60)
    bb.add(_tick(sec=37))  # 12:00:37 -> bucket 12:00:00
    bar = bb.flush("BTC/USDT")
    assert bar is not None and bar.start == T0


def test_out_of_order_tick_is_dropped() -> None:
    bb = BarBuilder(60)
    bb.add(_tick(sec=120, price="200"))  # opens bucket 12:02
    dropped = bb.add(_tick(sec=10, price="100"))  # belongs to 12:00 — already past
    assert dropped is None
    assert bb.dropped_out_of_order == 1
    bar = bb.flush("BTC/USDT")
    assert bar is not None and bar.open == Decimal("200")  # not corrupted by the late tick


def test_bid_ask_only_tick_uses_mid_price() -> None:
    bb = BarBuilder(60)
    bb.add(_tick(sec=1, price=None, volume=None, bid="100", ask="102"))
    bar = bb.flush("BTC/USDT")
    assert bar is not None and bar.open == Decimal("101")  # mid
    assert bar.volume == Decimal("0")  # no volume info -> 0


def test_symbols_are_independent() -> None:
    bb = BarBuilder(60)
    bb.add(_tick(sec=1, price="100", symbol="BTC/USDT"))
    bb.add(_tick(sec=1, price="50", symbol="ETH/USDT"))
    btc = bb.flush("BTC/USDT")
    eth = bb.flush("ETH/USDT")
    assert btc is not None and btc.open == Decimal("100")
    assert eth is not None and eth.open == Decimal("50")


def test_last_qty_used_when_volume_absent() -> None:
    bb = BarBuilder(60)
    tick = Tick(
        symbol="BTC/USDT",
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        ts=T0,
        last_price=Decimal("100"),
        last_qty=Decimal("2.5"),  # volume is None -> fall back to last_qty
    )
    bb.add(tick)
    bar = bb.flush("BTC/USDT")
    assert bar is not None and bar.volume == Decimal("2.5")
