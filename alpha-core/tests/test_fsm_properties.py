"""Property-based tests for the order FSM (P13.9).

Whatever the fill sequence, the FSM must conserve quantity (filled = Σ fills),
reach FILLED exactly when complete, and treat terminal states as absorbing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from alpha_core.core.enums import AssetClass, OrderState, OrderType, Side, Venue
from alpha_core.core.models import Fill, Order
from alpha_core.core.order_fsm import OrderEvent, OrderStateConflict
from alpha_core.core.order_fsm import step as _step

T0 = datetime(2026, 6, 15, 4, 0, tzinfo=UTC)


def step(order: Order, event: OrderEvent, **kw: Any) -> Order:
    """Test wrapper: supply the now=T0 stamp the FSM now requires (G23)."""
    return _step(order, event, now=T0, **kw)


def _open_order(quantity: Decimal) -> Order:
    return Order(
        client_order_id="alpha-x",
        venue_order_id="V1",
        symbol="NSE:RELIANCE",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=quantity,
        state=OrderState.OPEN,
        strategy_id="s1",
        created_at=T0,
        updated_at=T0,
    )


def _fill(fid: str, qty: Decimal) -> Fill:
    return Fill(
        fill_id=fid,
        client_order_id="alpha-x",
        symbol="NSE:RELIANCE",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        side=Side.BUY,
        quantity=qty,
        price=Decimal("100"),
        ts=T0,
    )


@given(deltas=st.lists(st.integers(min_value=1, max_value=50), min_size=1, max_size=12))
def test_partial_fills_conserve_quantity_and_complete(deltas: list[int]) -> None:
    total = Decimal(sum(deltas))
    order = _open_order(total)
    for i, d in enumerate(deltas):
        order = step(order, OrderEvent.FILL, fill=_fill(f"f{i}", Decimal(d)))
    assert order.filled_quantity == total
    assert order.state is OrderState.FILLED  # complete iff filled == quantity


@given(deltas=st.lists(st.integers(min_value=1, max_value=50), min_size=1, max_size=8))
def test_intermediate_fills_are_partial(deltas: list[int]) -> None:
    total = Decimal(sum(deltas)) + Decimal(10)  # leave 10 unfilled
    order = _open_order(total)
    for i, d in enumerate(deltas):
        order = step(order, OrderEvent.FILL, fill=_fill(f"f{i}", Decimal(d)))
    assert order.state is OrderState.PARTIALLY_FILLED
    assert order.filled_quantity < total


@given(qty=st.integers(min_value=1, max_value=100))
def test_overfill_after_complete_conflicts(qty: int) -> None:
    order = _open_order(Decimal(qty))
    order = step(order, OrderEvent.FILL, fill=_fill("f0", Decimal(qty)))  # fully filled
    assert order.state is OrderState.FILLED
    with pytest.raises(OrderStateConflict):  # terminal is absorbing — an extra fill conflicts
        step(order, OrderEvent.FILL, fill=_fill("f1", Decimal(1)))
