"""Position accounting tests (shared apply_fill)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from alpha_core.core.enums import AssetClass, Side, Venue
from alpha_core.core.models import Fill
from alpha_core.execution.positions import apply_fill

T0 = datetime(2026, 6, 15, 4, 0, tzinfo=UTC)


def _fill(side: Side, qty: str, price: str) -> Fill:
    return Fill(
        fill_id="f",
        client_order_id="o",
        symbol="X",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        ts=T0,
    )


def test_open_long() -> None:
    p = apply_fill(None, _fill(Side.BUY, "10", "100"))
    assert p.quantity == Decimal("10")
    assert p.average_price == Decimal("100")
    assert p.realized_pnl == 0


def test_increase_weighted_avg() -> None:
    p = apply_fill(None, _fill(Side.BUY, "10", "100"))
    p = apply_fill(p, _fill(Side.BUY, "10", "110"))
    assert p.quantity == Decimal("20")
    assert p.average_price == Decimal("105")


def test_reduce_realizes_pnl() -> None:
    p = apply_fill(None, _fill(Side.BUY, "10", "100"))
    p = apply_fill(p, _fill(Side.SELL, "4", "110"))
    assert p.quantity == Decimal("6")
    assert p.average_price == Decimal("100")  # unchanged on reduce
    assert p.realized_pnl == Decimal("40")  # (110-100)*4


def test_close_to_flat() -> None:
    p = apply_fill(None, _fill(Side.BUY, "10", "100"))
    p = apply_fill(p, _fill(Side.SELL, "10", "105"))
    assert p.quantity == 0
    assert p.average_price is None
    assert p.realized_pnl == Decimal("50")


def test_flip_past_zero() -> None:
    p = apply_fill(None, _fill(Side.BUY, "10", "100"))
    p = apply_fill(p, _fill(Side.SELL, "15", "110"))  # close 10, open 5 short
    assert p.quantity == Decimal("-5")
    assert p.average_price == Decimal("110")  # remainder opens at fill price
    assert p.realized_pnl == Decimal("100")  # (110-100)*10
