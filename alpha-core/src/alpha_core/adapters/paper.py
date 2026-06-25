"""PaperBroker — a simulated venue (ADR 0004). Built first; the golden harness.

Implements the ``BrokerAdapter`` contract identically to a real venue, with
simulated fills priced through the ``CostModel`` (ADR 0008) and in-memory
cash/position bookkeeping. It is the *venue side*: it does not run the core order
FSM (that is the OMS's job, Phase 5) — it accepts orders, simulates fills, and
emits normalized ``BrokerOrderEvent``s that the OMS will feed to the FSM.

Pricing: a quote per symbol is maintained via ``on_tick``. MARKET orders fill
immediately against the current quote; LIMIT orders fill if marketable, else rest
``OPEN`` and fill when a later tick crosses the limit. Idempotent on
``client_order_id`` — re-placing returns the existing ``venue_order_id`` with no
second order/fill.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from alpha_core.core.enums import OrderType, Side, Venue
from alpha_core.core.errors import OrderRejected, UnknownOrder
from alpha_core.core.interfaces import (
    BrokerAdapter,
    BrokerEventKind,
    BrokerOrderEvent,
    DataFeed,
)
from alpha_core.core.models import Fill, Order, Position, Tick
from alpha_core.execution.costs import CostBreakdown, CostModel, InstrumentMeta
from alpha_core.execution.positions import apply_fill
from alpha_core.observability.logging import get_logger

_STACK_FIELDS = ("brokerage", "stt", "exchange_txn", "gst", "sebi", "stamp_duty", "tds")


def _fees_only(b: CostBreakdown) -> Decimal:
    """Statutory fees (the price impact of spread/slippage is in the fill price)."""
    return sum((getattr(b, f) for f in _STACK_FIELDS), Decimal(0))


@dataclass
class _Resting:
    order: Order
    venue_order_id: str
    remaining: Decimal


@dataclass
class _Book:
    cash: Decimal
    positions: dict[tuple[Venue, str], Position] = field(default_factory=dict)


class PaperBroker(BrokerAdapter):
    """In-memory simulated broker."""

    def __init__(
        self,
        *,
        cost_model: CostModel,
        instruments: dict[str, InstrumentMeta],
        starting_cash: Decimal = Decimal("0"),
        feed: DataFeed | None = None,
        stress: bool = False,
    ) -> None:
        self._costs = cost_model
        self._instruments = instruments
        self._stress = stress  # 2x slippage stress test (ADR 0008)
        self._book = _Book(cash=starting_cash)
        self._feed = feed
        self._quotes: dict[str, Tick] = {}
        self._resting: dict[str, _Resting] = {}
        self._by_client: dict[str, str] = {}  # client_order_id -> venue_order_id
        self._ids = itertools.count(1)
        self._events: asyncio.Queue[BrokerOrderEvent] = asyncio.Queue()
        self.emitted: list[BrokerOrderEvent] = []  # synchronous mirror for tests
        self._log = get_logger("paper")

    # --- market data view ------------------------------------------------------

    def on_tick(self, tick: Tick) -> None:
        """Update the simulated market and try to fill resting orders."""
        self._quotes[tick.symbol] = tick
        for resting in list(self._resting.values()):
            if resting.order.symbol == tick.symbol and self._marketable(resting.order):
                self._fill(resting.order, resting.remaining)

    def _quote_prices(self, symbol: str) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        q = self._quotes.get(symbol)
        if q is None:
            return None, None, None
        return q.bid, q.ask, q.last_price

    def _marketable(self, order: Order) -> bool:
        bid, ask, ltp = self._quote_prices(order.symbol)
        if order.order_type is OrderType.MARKET:
            return bid is not None or ask is not None or ltp is not None
        if order.limit_price is None:
            return False
        ref_ask = ask if ask is not None else ltp
        ref_bid = bid if bid is not None else ltp
        if order.side is Side.BUY and ref_ask is not None:
            return ref_ask <= order.limit_price
        if order.side is Side.SELL and ref_bid is not None:
            return ref_bid >= order.limit_price
        return False

    # --- fills + bookkeeping ---------------------------------------------------

    def _fill(self, order: Order, quantity: Decimal) -> None:
        bid, ask, ltp = self._quote_prices(order.symbol)
        meta = self._instruments[order.symbol]
        breakdown = self._costs.estimate(
            side=order.side,
            quantity=quantity,
            bid=bid,
            ask=ask,
            ltp=ltp,
            instrument=meta,
            stress=self._stress,
        )
        price = breakdown.effective_fill_price
        fees = _fees_only(breakdown)
        n = next(self._ids)
        venue_order_id = self._by_client[order.client_order_id]
        fill = Fill(
            fill_id=f"PF{n}",
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            venue_fill_id=f"PVF{n}",
            symbol=order.symbol,
            venue=order.venue,
            asset_class=order.asset_class,
            side=order.side,
            quantity=quantity,
            price=price,
            fees=fees,
            ts=self._quotes[order.symbol].ts,
        )
        self._apply_to_book(fill)
        self._resting.pop(order.client_order_id, None)
        self._emit(
            BrokerOrderEvent(
                kind=BrokerEventKind.FILL,
                client_order_id=order.client_order_id,
                venue_order_id=venue_order_id,
                fill=fill,
            )
        )
        self._log.info(
            "paper_fill",
            client_order_id=order.client_order_id,
            qty=str(quantity),
            price=str(price),
            fees=str(fees),
        )

    def _apply_to_book(self, fill: Fill) -> None:
        cash_delta = (
            -(fill.quantity * fill.price) if fill.side is Side.BUY else (fill.quantity * fill.price)
        )
        self._book.cash += cash_delta - (fill.fees or Decimal(0))
        key = (fill.venue, fill.symbol)
        self._book.positions[key] = apply_fill(self._book.positions.get(key), fill)

    def _emit(self, event: BrokerOrderEvent) -> None:
        self.emitted.append(event)
        self._events.put_nowait(event)

    # --- BrokerAdapter ---------------------------------------------------------

    async def place_order(self, order: Order) -> str:
        if order.symbol not in self._instruments:
            raise OrderRejected("unknown instrument", reason="no instrument meta")
        existing = self._by_client.get(order.client_order_id)
        if existing is not None:
            return existing  # idempotent: no second order/fill
        # Reject *before* registering the client->venue id, so a rejected order
        # leaves no mapping behind — otherwise a later dedup-by-query
        # (``find_order_id``) would treat the rejected order as placed.
        if order.order_type is OrderType.MARKET and not self._marketable(order):
            raise OrderRejected("no market", reason="no quote to fill against")
        venue_order_id = f"P{next(self._ids)}"
        self._by_client[order.client_order_id] = venue_order_id
        if self._marketable(order):
            self._fill(order, order.quantity)
        else:
            self._resting[order.client_order_id] = _Resting(order, venue_order_id, order.quantity)
        return venue_order_id

    async def cancel(self, client_order_id: str) -> None:
        resting = self._resting.pop(client_order_id, None)
        if resting is None:
            # Idempotent: cancelling a filled/absent order is a safe no-op.
            if client_order_id not in self._by_client:
                raise UnknownOrder(client_order_id)
            return
        self._emit(
            BrokerOrderEvent(
                kind=BrokerEventKind.CANCEL,
                client_order_id=client_order_id,
                venue_order_id=resting.venue_order_id,
            )
        )

    async def modify(
        self,
        client_order_id: str,
        *,
        quantity: Decimal | None = None,
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
    ) -> None:
        resting = self._resting.get(client_order_id)
        if resting is None:
            raise UnknownOrder(client_order_id)
        updates: dict[str, object] = {}
        if limit_price is not None:
            updates["limit_price"] = limit_price
        if stop_price is not None:
            updates["stop_price"] = stop_price
        if quantity is not None:
            updates["quantity"] = quantity
            resting.remaining = quantity
        resting.order = resting.order.model_copy(update=updates)
        if self._marketable(resting.order):
            self._fill(resting.order, resting.remaining)

    async def get_positions(self) -> list[Position]:
        return list(self._book.positions.values())

    async def get_orders(self) -> list[Order]:
        return [r.order for r in self._resting.values()]

    async def find_order_id(self, client_order_id: str) -> str | None:
        return self._by_client.get(client_order_id)

    async def subscribe_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        if self._feed is None:
            raise RuntimeError("PaperBroker has no feed to subscribe to")
        async for tick in self._feed.stream_ticks(symbols):
            self.on_tick(tick)
            yield tick

    async def order_events(self) -> AsyncIterator[BrokerOrderEvent]:
        while not self._events.empty():
            yield self._events.get_nowait()

    @property
    def cash(self) -> Decimal:
        return self._book.cash
