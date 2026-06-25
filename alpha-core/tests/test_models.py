"""Core model validation tests (ADR 0002)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from alpha_core.core.enums import AssetClass, OrderState, OrderType, Side, Venue
from alpha_core.core.models import Bar, Fill, Order, Position, Signal, TargetExposure, Tick

T0 = datetime(2026, 1, 1, 9, 15, tzinfo=UTC)


def _order(**overrides: object) -> Order:
    base: dict[str, object] = {
        "client_order_id": "vega-1",
        "symbol": "NSE:RELIANCE",
        "venue": Venue.NSE,
        "asset_class": AssetClass.EQUITY,
        "side": Side.BUY,
        "order_type": OrderType.MARKET,
        "quantity": Decimal("10"),
        "state": OrderState.NEW,
        "strategy_id": "s1",
        "created_at": T0,
        "updated_at": T0,
    }
    base.update(overrides)
    return Order(**base)  # type: ignore[arg-type]


# --- money/quantity is never float ---------------------------------------------


def test_float_money_rejected() -> None:
    with pytest.raises(ValidationError):
        _order(quantity=10.0)


def test_decimal_int_str_money_accepted() -> None:
    assert _order(quantity=Decimal("10")).quantity == Decimal("10")
    assert _order(quantity=10).quantity == Decimal("10")
    assert _order(quantity="10").quantity == Decimal("10")


# --- timestamps are tz-aware UTC -----------------------------------------------


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError):
        _order(created_at=datetime(2026, 1, 1, 9, 15))


def test_datetime_normalized_to_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    o = _order(created_at=datetime(2026, 1, 1, 14, 45, tzinfo=ist))
    assert o.created_at == T0
    assert o.created_at.tzinfo == UTC


# --- positivity ----------------------------------------------------------------


def test_non_positive_quantity_rejected() -> None:
    with pytest.raises(ValidationError):
        _order(quantity=Decimal("0"))
    with pytest.raises(ValidationError):
        _order(quantity=Decimal("-1"))


# --- Order presence + consistency rules ----------------------------------------


def test_limit_requires_limit_price() -> None:
    with pytest.raises(ValidationError):
        _order(order_type=OrderType.LIMIT)
    assert _order(order_type=OrderType.LIMIT, limit_price=Decimal("100")).limit_price == Decimal(
        "100"
    )


def test_market_must_not_carry_prices() -> None:
    with pytest.raises(ValidationError):
        _order(order_type=OrderType.MARKET, limit_price=Decimal("100"))


def test_stop_limit_requires_both() -> None:
    with pytest.raises(ValidationError):
        _order(order_type=OrderType.STOP_LIMIT, limit_price=Decimal("100"))
    ok = _order(
        order_type=OrderType.STOP_LIMIT, limit_price=Decimal("100"), stop_price=Decimal("99")
    )
    assert ok.stop_price == Decimal("99")


def test_filled_le_quantity() -> None:
    with pytest.raises(ValidationError):
        _order(filled_quantity=Decimal("11"), average_fill_price=Decimal("100"))


def test_avg_price_iff_filled() -> None:
    with pytest.raises(ValidationError):
        _order(filled_quantity=Decimal("5"))  # filled but no avg price
    with pytest.raises(ValidationError):
        _order(average_fill_price=Decimal("100"))  # avg price but unfilled
    ok = _order(filled_quantity=Decimal("5"), average_fill_price=Decimal("100"))
    assert ok.filled_quantity == Decimal("5")


# --- Bar / Tick / Position / Signal -------------------------------------------


def test_bar_ohlc_ordering() -> None:
    common = {
        "symbol": "NSE:RELIANCE",
        "venue": Venue.NSE,
        "asset_class": AssetClass.EQUITY,
        "start": T0,
        "interval": timedelta(minutes=1),
        "volume": Decimal("100"),
    }
    Bar(open=Decimal("10"), high=Decimal("12"), low=Decimal("9"), close=Decimal("11"), **common)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Bar(open=Decimal("10"), high=Decimal("9"), low=Decimal("9"), close=Decimal("11"), **common)  # type: ignore[arg-type]


def test_tick_presence_rule() -> None:
    common = {"symbol": "X", "venue": Venue.NSE, "asset_class": AssetClass.EQUITY, "ts": T0}
    Tick(last_price=Decimal("10"), **common)  # type: ignore[arg-type]
    Tick(bid=Decimal("10"), ask=Decimal("11"), **common)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Tick(bid=Decimal("10"), **common)  # type: ignore[arg-type]


def test_position_avg_iff_nonzero() -> None:
    common = {"symbol": "X", "venue": Venue.NSE, "asset_class": AssetClass.EQUITY, "updated_at": T0}
    Position(quantity=Decimal("0"), **common)  # type: ignore[arg-type]
    Position(quantity=Decimal("-5"), average_price=Decimal("100"), **common)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Position(quantity=Decimal("5"), **common)  # type: ignore[arg-type]


def test_signal_presence_rule() -> None:
    common = {
        "strategy_id": "s1",
        "symbol": "X",
        "asset_class": AssetClass.EQUITY,
        "side": Side.BUY,
        "quantity": Decimal("1"),
        "created_at": T0,
    }
    Signal(order_type=OrderType.MARKET, **common)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Signal(order_type=OrderType.LIMIT, **common)  # type: ignore[arg-type]


def _signal(**overrides: object) -> Signal:
    base: dict[str, object] = {
        "strategy_id": "s1",
        "symbol": "X",
        "asset_class": AssetClass.EQUITY,
        "side": Side.BUY,
        "quantity": Decimal("1"),
        "order_type": OrderType.MARKET,
        "created_at": T0,
    }
    base.update(overrides)
    return Signal(**base)  # type: ignore[arg-type]


# --- Signal.score (conviction for ranking, R1) --------------------------------


def test_signal_score_defaults_none() -> None:
    assert _signal().score is None


def test_signal_score_accepts_decimal_int_str() -> None:
    assert _signal(score=Decimal("0.5")).score == Decimal("0.5")
    assert _signal(score=2).score == Decimal("2")
    assert _signal(score="1.25").score == Decimal("1.25")


def test_signal_score_rejects_float() -> None:
    with pytest.raises(ValidationError):
        _signal(score=0.5)


def test_signal_score_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        _signal(score=Decimal("-1"))


@given(st.integers(min_value=0, max_value=1000), st.integers(min_value=0, max_value=1000))
def test_signal_score_orders_like_decimal(a: int, b: int) -> None:
    """Scores rank exactly as their Decimal values — the property ranking needs."""
    sa, sb = _signal(score=a), _signal(score=b)
    assert (sa.score < sb.score) == (Decimal(a) < Decimal(b))  # type: ignore[operator]


# --- TargetExposure (signed capital fraction, R1) -----------------------------


def _target(**overrides: object) -> TargetExposure:
    base: dict[str, object] = {
        "strategy_id": "s1",
        "symbol": "X",
        "asset_class": AssetClass.EQUITY,
        "weight": Decimal("0.1"),
    }
    base.update(overrides)
    return TargetExposure(**base)  # type: ignore[arg-type]


def test_target_exposure_signed_and_zero() -> None:
    assert _target(weight=Decimal("0.25")).weight == Decimal("0.25")
    assert _target(weight=Decimal("-0.4")).weight == Decimal("-0.4")
    assert _target(weight=0).weight == Decimal("0")


def test_target_exposure_rejects_float_weight() -> None:
    with pytest.raises(ValidationError):
        _target(weight=0.1)


def test_target_exposure_immutable() -> None:
    with pytest.raises(ValidationError):
        _target().weight = Decimal("0.2")


def test_fill_positive_price_qty() -> None:
    common = {
        "fill_id": "f1",
        "client_order_id": "vega-1",
        "symbol": "X",
        "venue": Venue.NSE,
        "asset_class": AssetClass.EQUITY,
        "side": Side.BUY,
        "ts": T0,
    }
    Fill(quantity=Decimal("1"), price=Decimal("100"), **common)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Fill(quantity=Decimal("0"), price=Decimal("100"), **common)  # type: ignore[arg-type]


def test_frozen_models_immutable() -> None:
    fill = Fill(
        fill_id="f1",
        client_order_id="vega-1",
        symbol="X",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("100"),
        ts=T0,
    )
    with pytest.raises(ValidationError):
        fill.price = Decimal("200")


# --- property: integer/str quantities round-trip to exact Decimal --------------


@given(st.integers(min_value=1, max_value=10_000))
def test_quantity_int_roundtrip(qty: int) -> None:
    o = _order(quantity=qty)
    assert o.quantity == Decimal(qty)
    assert isinstance(o.quantity, Decimal)
