"""OMS async event-stream consumer tests (Phase 7.5 H5)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

import pytest

from alpha_core.core.enums import Venue
from alpha_core.core.interfaces import (
    BrokerAdapter,
    BrokerEventKind,
    BrokerOrderEvent,
)
from alpha_core.core.models import Order, Position, Tick
from alpha_core.execution.oms import OMS
from alpha_core.execution.state import StateStore
from alpha_core.risk.limits import RiskConfig
from alpha_core.risk.manager import KillTrigger, RiskManager

T0 = datetime(2026, 6, 15, 4, 0, tzinfo=UTC)


def _risk() -> RiskManager:
    cfg = RiskConfig.model_validate(
        {
            "base_capital": "100000",
            "limits": {
                "max_gross_exposure": "1.00",
                "max_position_per_instrument": "0.20",
                "max_concurrent_positions": 5,
                "max_order_value": "0.25",
                "max_orders_per_minute": 10,
                "max_daily_loss_halt": "0.02",
                "max_loss_per_trade": "0.01",
                "per_segment_exposure_cap": "0.60",
            },
        }
    )
    return RiskManager(cfg)


class _ForeverStream(BrokerAdapter):
    """A live-style adapter whose order_events() blocks for new events forever."""

    def __init__(self) -> None:
        self._q: asyncio.Queue[BrokerOrderEvent] = asyncio.Queue()

    def push(self, event: BrokerOrderEvent) -> None:
        self._q.put_nowait(event)

    async def order_events(self) -> AsyncIterator[BrokerOrderEvent]:
        while True:
            yield await self._q.get()

    async def place_order(self, order: Order) -> str:
        raise NotImplementedError

    async def cancel(self, client_order_id: str) -> None:
        raise NotImplementedError

    async def modify(self, client_order_id: str, **kwargs: object) -> None:
        raise NotImplementedError

    async def get_positions(self) -> list[Position]:
        return []

    async def get_orders(self) -> list[Order]:
        return []

    async def find_order_id(self, client_order_id: str) -> str | None:
        return None

    def subscribe_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        raise NotImplementedError


async def test_consume_events_processes_stream_then_cancels() -> None:
    risk = _risk()
    adapter = _ForeverStream()
    store = StateStore("sqlite:///:memory:")
    store.create_schema()
    oms = OMS(adapter=adapter, risk=risk, store=store, venue=Venue.NSE)

    task = asyncio.create_task(oms.consume_events())
    # an event for an unknown order -> handled -> trips the kill switch
    adapter.push(BrokerOrderEvent(kind=BrokerEventKind.FILL, client_order_id="ghost"))
    for _ in range(50):  # let the consumer run
        if risk.is_halted:
            break
        await asyncio.sleep(0.005)
    assert risk.is_halted is True
    assert risk.halt_trigger is KillTrigger.RECONCILIATION_MISMATCH

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_consume_events_drains_bounded_stream() -> None:
    # a bounded adapter (order_events ends) -> consume_events returns
    class _Bounded(_ForeverStream):
        async def order_events(self) -> AsyncIterator[BrokerOrderEvent]:
            while not self._q.empty():
                yield self._q.get_nowait()

    risk = _risk()
    adapter = _Bounded()
    store = StateStore("sqlite:///:memory:")
    store.create_schema()
    oms = OMS(adapter=adapter, risk=risk, store=store, venue=Venue.NSE)
    adapter.push(BrokerOrderEvent(kind=BrokerEventKind.FILL, client_order_id="ghost"))
    await oms.consume_events()  # returns (does not block)
    assert risk.is_halted is True
