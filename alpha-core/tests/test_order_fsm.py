"""Order FSM tests (ADR 0003) — exhaustive 48-cell coverage + fill scenarios."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from alpha_core.core.enums import AssetClass, OrderState, OrderType, Side, Venue
from alpha_core.core.models import Fill, Order
from alpha_core.core.order_fsm import (
    IllegalOrderTransition,
    OrderEvent,
    OrderStateConflict,
)
from alpha_core.core.order_fsm import step as _step

T0 = datetime(2026, 1, 1, 9, 15, tzinfo=UTC)
QTY = Decimal("10")


def step(order: Order, event: OrderEvent, **kw: Any) -> Order:
    """Test wrapper: supply the now=T0 stamp the FSM now requires (G23)."""
    return _step(order, event, now=T0, **kw)


def _order_in(state: OrderState) -> Order:
    filled = Decimal("0")
    avg: Decimal | None = None
    if state is OrderState.PARTIALLY_FILLED:
        filled, avg = Decimal("4"), Decimal("100")
    elif state is OrderState.FILLED:
        filled, avg = QTY, Decimal("100")
    return Order(
        client_order_id="vega-1",
        symbol="NSE:RELIANCE",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=QTY,
        state=state,
        filled_quantity=filled,
        average_fill_price=avg,
        strategy_id="s1",
        created_at=T0,
        updated_at=T0,
    )


def _fill(qty: str, fid: str = "f1", vfid: str | None = "vf1") -> Fill:
    return Fill(
        fill_id=fid,
        client_order_id="vega-1",
        venue_fill_id=vfid,
        symbol="NSE:RELIANCE",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        side=Side.BUY,
        quantity=Decimal(qty),
        price=Decimal("100"),
        ts=T0,
    )


_S, _E = OrderState, OrderEvent
# Expected outcome per cell: a sentinel string or the resulting OrderState.
# FILL-class cells use a 2-unit partial fill, so they settle to PARTIALLY_FILLED.
EXPECTED: dict[OrderState, dict[OrderEvent, object]] = {
    _S.NEW: {
        _E.SUBMIT: _S.PENDING,
        _E.ACK: "CONFLICT",
        _E.FILL: "CONFLICT",
        _E.REJECT: "CONFLICT",
        _E.CANCEL: _S.CANCELLED,
        _E.EXPIRE: "CONFLICT",
    },
    _S.PENDING: {
        _E.SUBMIT: "BUG",
        _E.ACK: _S.OPEN,
        _E.FILL: _S.PARTIALLY_FILLED,
        _E.REJECT: _S.REJECTED,
        _E.CANCEL: _S.CANCELLED,
        _E.EXPIRE: _S.EXPIRED,
    },
    _S.OPEN: {
        _E.SUBMIT: "BUG",
        _E.ACK: "NOOP",
        _E.FILL: _S.PARTIALLY_FILLED,
        _E.REJECT: _S.REJECTED,
        _E.CANCEL: _S.CANCELLED,
        _E.EXPIRE: _S.EXPIRED,
    },
    _S.PARTIALLY_FILLED: {
        _E.SUBMIT: "BUG",
        _E.ACK: "NOOP",
        _E.FILL: _S.PARTIALLY_FILLED,
        _E.REJECT: "CONFLICT",
        _E.CANCEL: _S.CANCELLED,
        _E.EXPIRE: _S.EXPIRED,
    },
    _S.FILLED: {
        _E.SUBMIT: "BUG",
        _E.ACK: "NOOP",
        _E.FILL: "CONFLICT",
        _E.REJECT: "CONFLICT",
        _E.CANCEL: "CONFLICT",
        _E.EXPIRE: "CONFLICT",
    },
    _S.REJECTED: {
        _E.SUBMIT: "BUG",
        _E.ACK: "NOOP",
        _E.FILL: "CONFLICT",
        _E.REJECT: "NOOP",
        _E.CANCEL: "CONFLICT",
        _E.EXPIRE: "CONFLICT",
    },
    _S.CANCELLED: {
        _E.SUBMIT: "BUG",
        _E.ACK: "NOOP",
        _E.FILL: "CONFLICT",
        _E.REJECT: "CONFLICT",
        _E.CANCEL: "NOOP",
        _E.EXPIRE: "CONFLICT",
    },
    _S.EXPIRED: {
        _E.SUBMIT: "BUG",
        _E.ACK: "NOOP",
        _E.FILL: "CONFLICT",
        _E.REJECT: "CONFLICT",
        _E.CANCEL: "CONFLICT",
        _E.EXPIRE: "NOOP",
    },
}


@pytest.mark.parametrize("state", list(OrderState))
@pytest.mark.parametrize("event", list(OrderEvent))
def test_every_cell(state: OrderState, event: OrderEvent) -> None:
    order = _order_in(state)
    expected = EXPECTED[state][event]
    fill = _fill("2") if event is OrderEvent.FILL else None

    if expected == "BUG":
        with pytest.raises(IllegalOrderTransition):
            step(order, event, fill=fill)
    elif expected == "CONFLICT":
        with pytest.raises(OrderStateConflict):
            step(order, event, fill=fill)
    elif expected == "NOOP":
        result = step(order, event, fill=fill)
        assert result is order
        assert result.state is state
    else:  # a concrete target OrderState
        result = step(order, event, fill=fill)
        assert result.state is expected


def test_table_is_total() -> None:
    assert len(OrderState) * len(OrderEvent) == 48
    for state in OrderState:
        assert set(EXPECTED[state]) == set(OrderEvent)


# --- targeted scenarios --------------------------------------------------------


def test_partial_then_complete_weighted_avg() -> None:
    order = _order_in(OrderState.OPEN)
    order = step(order, OrderEvent.FILL, fill=_fill("4", vfid="a"))
    assert order.state is OrderState.PARTIALLY_FILLED
    assert order.filled_quantity == Decimal("4")
    order = step(
        order,
        OrderEvent.FILL,
        fill=Fill(
            fill_id="f2",
            client_order_id="vega-1",
            venue_fill_id="b",
            symbol="NSE:RELIANCE",
            venue=Venue.NSE,
            asset_class=AssetClass.EQUITY,
            side=Side.BUY,
            quantity=Decimal("6"),
            price=Decimal("110"),
            ts=T0,
        ),
    )
    assert order.state is OrderState.FILLED
    assert order.filled_quantity == QTY
    assert order.average_fill_price == Decimal("106")  # (100*4 + 110*6)/10


def test_over_fill_conflict() -> None:
    order = _order_in(OrderState.OPEN)
    with pytest.raises(OrderStateConflict):
        step(order, OrderEvent.FILL, fill=_fill("11"))


def test_duplicate_fill_deduped() -> None:
    seen: set[str] = set()
    order = _order_in(OrderState.OPEN)
    order = step(order, OrderEvent.FILL, fill=_fill("2", vfid="dup"), seen_fills=seen)
    assert order.filled_quantity == Decimal("2")
    again = step(order, OrderEvent.FILL, fill=_fill("2", vfid="dup"), seen_fills=seen)
    assert again is order  # deduped no-op
    assert again.filled_quantity == Decimal("2")


def test_fill_before_ack_implies_ack_and_sets_venue_id() -> None:
    order = _order_in(OrderState.PENDING)
    fill = Fill(
        fill_id="f1",
        client_order_id="vega-1",
        venue_order_id="V1",
        venue_fill_id="vf1",
        symbol="NSE:RELIANCE",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        side=Side.BUY,
        quantity=Decimal("2"),
        price=Decimal("100"),
        ts=T0,
    )
    order = step(order, OrderEvent.FILL, fill=fill)
    assert order.state is OrderState.PARTIALLY_FILLED
    assert order.venue_order_id == "V1"


def test_ack_records_venue_order_id() -> None:
    order = _order_in(OrderState.PENDING)
    order = step(order, OrderEvent.ACK, venue_order_id="V9")
    assert order.state is OrderState.OPEN
    assert order.venue_order_id == "V9"


def test_submit_only_from_new() -> None:
    assert step(_order_in(OrderState.NEW), OrderEvent.SUBMIT).state is OrderState.PENDING
    with pytest.raises(IllegalOrderTransition):
        step(_order_in(OrderState.OPEN), OrderEvent.SUBMIT)


def test_fill_after_cancel_conflict() -> None:
    with pytest.raises(OrderStateConflict):
        step(_order_in(OrderState.CANCELLED), OrderEvent.FILL, fill=_fill("1"))
