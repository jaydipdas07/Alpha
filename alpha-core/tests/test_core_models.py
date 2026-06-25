"""Core model invariants: Decimal-only money, tz-aware UTC, immutability, OHLC sanity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from alpha_core.core.enums import AssetClass, OrderType, Side, Venue
from alpha_core.core.models import Bar, Signal, Tick

_START = datetime(2026, 1, 1, tzinfo=UTC)
_INTERVAL = timedelta(minutes=5)

_BASE_BAR: dict[str, Any] = {
    "symbol": "BTCUSDT",
    "venue": Venue.BINANCE,
    "asset_class": AssetClass.CRYPTO,
    "start": _START,
    "interval": _INTERVAL,
    "open": Decimal("100"),
    "high": Decimal("110"),
    "low": Decimal("90"),
    "close": Decimal("105"),
    "volume": Decimal("1000"),
}


def _bar(**over: Any) -> Bar:
    return Bar(**{**_BASE_BAR, **over})


def test_bar_valid() -> None:
    bar = _bar()
    assert bar.close == Decimal("105")
    assert bar.start.tzinfo is UTC


def test_bar_rejects_float_price() -> None:
    with pytest.raises(ValidationError, match="never float"):
        _bar(close=105.0)


def test_bar_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _bar(start=datetime(2026, 1, 1))  # naive


def test_bar_normalizes_to_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    bar = _bar(start=datetime(2026, 1, 1, 5, 30, tzinfo=ist))
    assert bar.start == _START  # 05:30 IST == 00:00 UTC
    assert bar.start.tzinfo is UTC


def test_bar_high_must_dominate() -> None:
    with pytest.raises(ValidationError, match="high must be"):
        _bar(high=Decimal("100"))  # high < close (105)


def test_bar_low_must_be_lowest() -> None:
    with pytest.raises(ValidationError, match="low must be"):
        _bar(low=Decimal("106"))  # low > close (105)


def test_bar_interval_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="interval must be positive"):
        _bar(interval=timedelta(0))


def test_bar_rejects_nonpositive_price() -> None:
    with pytest.raises(ValidationError):
        _bar(open=Decimal("0"))  # PosMoney is gt=0


def test_bar_is_frozen() -> None:
    bar = _bar()
    with pytest.raises(ValidationError):
        bar.close = Decimal("1")


def test_tick_valid() -> None:
    tick = Tick(
        symbol="BTCUSDT",
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        ts=_START,
        last_price=Decimal("100"),
    )
    assert tick.last_price == Decimal("100")


def test_tick_rejects_float() -> None:
    with pytest.raises(ValidationError, match="never float"):
        Tick(
            symbol="BTCUSDT",
            venue=Venue.BINANCE,
            asset_class=AssetClass.CRYPTO,
            ts=_START,
            last_price=100.0,  # type: ignore[arg-type]  # intentional: float must be rejected
        )


def test_signal_defaults() -> None:
    sig = Signal(
        strategy_id="s",
        symbol="BTCUSDT",
        asset_class=AssetClass.CRYPTO,
        side=Side.BUY,
        quantity=Decimal("1"),
        created_at=_START,
    )
    assert sig.order_type is OrderType.MARKET
    assert sig.reason is None
    assert sig.score is None


def test_signal_rejects_nonpositive_qty() -> None:
    with pytest.raises(ValidationError):
        Signal(
            strategy_id="s",
            symbol="X",
            asset_class=AssetClass.CRYPTO,
            side=Side.SELL,
            quantity=Decimal("0"),  # PosQty is gt=0
            created_at=_START,
        )


def test_signal_score_must_be_nonnegative() -> None:
    with pytest.raises(ValidationError):
        Signal(
            strategy_id="s",
            symbol="X",
            asset_class=AssetClass.CRYPTO,
            side=Side.BUY,
            quantity=Decimal("1"),
            created_at=_START,
            score=Decimal("-1"),  # Score is ge=0
        )
