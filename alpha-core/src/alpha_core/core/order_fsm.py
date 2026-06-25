"""Order state machine — the total transition table from ADR 0003.

8 states x 6 events = 48 cells, every one defined: a transition, an idempotent
no-op, or a named raise. Two raise classes:
- ``IllegalOrderTransition`` — an OMS contract violation (a code bug), fail fast.
- ``OrderStateConflict`` — a broker callback that contradicts local truth; the
  OMS routes it to reconciliation (ADR 0009).

Pre-FSM gates (ADR 0003): order lookup is the OMS's job (``UnknownOrderError``);
fill dedup by ``venue_fill_id`` is handled here when ``seen_fills`` is supplied.

Transitions are applied functionally: each ``step`` returns a freshly *validated*
``Order`` (or the same instance for a no-op), so every transition re-checks the
ADR 0002 invariants and there is never a half-updated order.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum, StrEnum, auto

from alpha_core.core.enums import OrderState
from alpha_core.core.models import Fill, Order


class OrderEvent(StrEnum):
    """The six events that drive an order (ADR 0003)."""

    SUBMIT = "SUBMIT"  # OMS-internal action
    ACK = "ACK"  # broker callback
    FILL = "FILL"  # broker callback
    REJECT = "REJECT"  # broker callback
    CANCEL = "CANCEL"  # broker callback, or local drop of a NEW order
    EXPIRE = "EXPIRE"  # broker callback


class OrderFSMError(Exception):
    """Base for FSM errors."""


class IllegalOrderTransition(OrderFSMError):
    """An OMS contract violation (e.g. SUBMIT in a non-NEW state) — fail fast."""

    def __init__(self, state: OrderState, event: OrderEvent) -> None:
        super().__init__(f"illegal transition: {event} in {state}")
        self.state = state
        self.event = event


class OrderStateConflict(OrderFSMError):
    """A broker callback contradicts local truth — route to reconciliation."""

    def __init__(self, state: OrderState, event: OrderEvent, detail: str = "") -> None:
        msg = f"order-state conflict: {event} in {state}"
        super().__init__(f"{msg} ({detail})" if detail else msg)
        self.state = state
        self.event = event
        self.detail = detail


class UnknownOrderError(OrderFSMError):
    """A broker callback references an order not in the local store (pre-FSM gate)."""


class _Cell(Enum):
    """Non-transition outcomes in the table."""

    NOOP = auto()
    BUG = auto()  # -> IllegalOrderTransition
    CONFLICT = auto()  # -> OrderStateConflict
    FILL = auto()  # -> apply the fill-quantity rule


Cell = OrderState | _Cell

_S = OrderState
_E = OrderEvent

# The total 48-cell transition table (ADR 0003).
_TABLE: dict[OrderState, dict[OrderEvent, Cell]] = {
    _S.NEW: {
        _E.SUBMIT: _S.PENDING,
        _E.ACK: _Cell.CONFLICT,
        _E.FILL: _Cell.CONFLICT,
        _E.REJECT: _Cell.CONFLICT,
        _E.CANCEL: _S.CANCELLED,  # local cancel of a never-sent order
        _E.EXPIRE: _Cell.CONFLICT,
    },
    _S.PENDING: {
        _E.SUBMIT: _Cell.BUG,
        _E.ACK: _S.OPEN,
        _E.FILL: _Cell.FILL,  # implies the ack
        _E.REJECT: _S.REJECTED,
        _E.CANCEL: _S.CANCELLED,
        _E.EXPIRE: _S.EXPIRED,
    },
    _S.OPEN: {
        _E.SUBMIT: _Cell.BUG,
        _E.ACK: _Cell.NOOP,
        _E.FILL: _Cell.FILL,
        _E.REJECT: _S.REJECTED,  # reject-after-ack is legal
        _E.CANCEL: _S.CANCELLED,
        _E.EXPIRE: _S.EXPIRED,
    },
    _S.PARTIALLY_FILLED: {
        _E.SUBMIT: _Cell.BUG,
        _E.ACK: _Cell.NOOP,
        _E.FILL: _Cell.FILL,
        _E.REJECT: _Cell.CONFLICT,  # cannot un-execute filled qty
        _E.CANCEL: _S.CANCELLED,
        _E.EXPIRE: _S.EXPIRED,
    },
    _S.FILLED: {
        _E.SUBMIT: _Cell.BUG,
        _E.ACK: _Cell.NOOP,
        _E.FILL: _Cell.CONFLICT,  # over-fill / fill-after-fill
        _E.REJECT: _Cell.CONFLICT,
        _E.CANCEL: _Cell.CONFLICT,
        _E.EXPIRE: _Cell.CONFLICT,
    },
    _S.REJECTED: {
        _E.SUBMIT: _Cell.BUG,
        _E.ACK: _Cell.NOOP,
        _E.FILL: _Cell.CONFLICT,
        _E.REJECT: _Cell.NOOP,
        _E.CANCEL: _Cell.CONFLICT,
        _E.EXPIRE: _Cell.CONFLICT,
    },
    _S.CANCELLED: {
        _E.SUBMIT: _Cell.BUG,
        _E.ACK: _Cell.NOOP,
        _E.FILL: _Cell.CONFLICT,  # fill-after-cancel
        _E.REJECT: _Cell.CONFLICT,
        _E.CANCEL: _Cell.NOOP,
        _E.EXPIRE: _Cell.CONFLICT,
    },
    _S.EXPIRED: {
        _E.SUBMIT: _Cell.BUG,
        _E.ACK: _Cell.NOOP,
        _E.FILL: _Cell.CONFLICT,
        _E.REJECT: _Cell.CONFLICT,
        _E.CANCEL: _Cell.CONFLICT,
        _E.EXPIRE: _Cell.NOOP,
    },
}


def _with(order: Order, now: datetime, **changes: object) -> Order:
    """Return a re-validated copy of ``order`` with ``changes`` applied and the
    ``updated_at`` stamp set to ``now`` (the caller's clock — bar time in
    backtest, wall-clock live; never sourced here, to keep backtest≡live)."""
    data = order.model_dump()
    data.update(changes)
    data["updated_at"] = now
    return Order.model_validate(data)


def _apply_fill(order: Order, fill: Fill, now: datetime) -> Order:
    """Apply the fill-quantity rule (ADR 0003); over-fill raises a conflict."""
    remaining = order.quantity - order.filled_quantity
    if fill.quantity > remaining:
        raise OrderStateConflict(order.state, OrderEvent.FILL, "over-fill")
    new_filled = order.filled_quantity + fill.quantity
    if order.average_fill_price is None:
        new_avg = fill.price
    else:
        prior = order.average_fill_price * order.filled_quantity
        new_avg = (prior + fill.price * fill.quantity) / new_filled
    state = OrderState.FILLED if new_filled == order.quantity else OrderState.PARTIALLY_FILLED
    changes: dict[str, object] = {
        "filled_quantity": new_filled,
        "average_fill_price": new_avg,
        "state": state,
    }
    if order.venue_order_id is None and fill.venue_order_id is not None:
        changes["venue_order_id"] = fill.venue_order_id
    return _with(order, now, **changes)


def _fill_key(fill: Fill) -> str:
    return fill.venue_fill_id or fill.fill_id


def step(
    order: Order,
    event: OrderEvent,
    *,
    now: datetime,
    venue_order_id: str | None = None,
    fill: Fill | None = None,
    seen_fills: set[str] | None = None,
) -> Order:
    """Apply ``event`` to ``order`` per the ADR 0003 table.

    Returns the resulting (validated) ``Order`` — the same instance for a no-op,
    a new instance for a transition. Raises ``IllegalOrderTransition`` or
    ``OrderStateConflict`` for the named illegal cells. ``now`` stamps
    ``updated_at`` and must come from the caller's clock (bar time in backtest)
    so the FSM never sources wall-clock — preserving backtest≡live (G23).
    """
    if event is OrderEvent.FILL:
        if fill is None:
            raise ValueError("FILL event requires a fill")
        # Pre-FSM dedup gate: a duplicate fill is dropped before the table.
        if seen_fills is not None and _fill_key(fill) in seen_fills:
            return order

    cell = _TABLE[order.state][event]

    if cell is _Cell.BUG:
        raise IllegalOrderTransition(order.state, event)
    if cell is _Cell.CONFLICT:
        raise OrderStateConflict(order.state, event)
    if cell is _Cell.NOOP:
        return order
    if cell is _Cell.FILL:
        assert fill is not None  # guaranteed above
        updated = _apply_fill(order, fill, now)
        if seen_fills is not None:
            seen_fills.add(_fill_key(fill))
        return updated

    # cell is a concrete target OrderState.
    changes: dict[str, object] = {"state": cell}
    if event is OrderEvent.ACK and venue_order_id is not None:
        changes["venue_order_id"] = venue_order_id
    return _with(order, now, **changes)
