"""Strategy engine — wires a feed to a strategy and streams signals (ADR 0001).

Venue-agnostic: it depends only on the core `DataFeed`/`Strategy` ABCs and the
domain models, never a broker SDK. It pulls normalized bars/ticks from the feed,
hands each to the strategy, and yields the abstract `Signal`s the strategy emits.
Downstream (Phase 5) those signals pass through the risk gate and the OMS — the
engine itself never places orders.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from alpha_core.core.interfaces import DataFeed, Strategy
from alpha_core.core.models import Bar, Signal, Tick


class StrategyEngine:
    """Drives one `Strategy` over a `DataFeed`, emitting its signals."""

    def __init__(self, strategy: Strategy) -> None:
        self._strategy = strategy

    def process_bar(self, bar: Bar) -> Sequence[Signal]:
        """Hand one bar to the strategy and return its signals (for app loops)."""
        return self._strategy.on_bar(bar)

    def process_tick(self, tick: Tick) -> Sequence[Signal]:
        """Hand one tick to the strategy and return its signals."""
        return self._strategy.on_tick(tick)

    async def run_bars(self, feed: DataFeed, symbols: Sequence[str]) -> AsyncIterator[Signal]:
        """Stream bars through `strategy.on_bar`, yielding each emitted signal."""
        async for bar in feed.stream_bars(symbols):
            for signal in self._strategy.on_bar(bar):
                yield signal

    async def run_ticks(self, feed: DataFeed, symbols: Sequence[str]) -> AsyncIterator[Signal]:
        """Stream ticks through `strategy.on_tick`, yielding each emitted signal."""
        async for tick in feed.stream_ticks(symbols):
            for signal in self._strategy.on_tick(tick):
                yield signal
