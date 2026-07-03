"""Replay data feed (ADR 0001/0004).

``ReplayFeed`` implements the ``DataFeed`` contract by streaming a pre-built,
ordered sequence of ticks/bars (from CSV via ``historical`` or synthetic). It is
deterministic — the same input yields the same stream every run — which makes it
the backbone of replay tests and the paper loop. It filters by subscribed symbol
and preserves input order (the caller sorts by time).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable, Sequence

from alpha_core.core.interfaces import BrokerAdapter, DataFeed
from alpha_core.core.models import Bar, Tick


class ReplayFeed(DataFeed):
    """A deterministic feed over in-memory ticks/bars."""

    def __init__(
        self,
        *,
        ticks: Iterable[Tick] = (),
        bars: Iterable[Bar] = (),
    ) -> None:
        self._ticks: list[Tick] = list(ticks)
        self._bars: list[Bar] = list(bars)

    async def stream_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        wanted = set(symbols)
        for tick in self._ticks:
            if tick.symbol in wanted:
                yield tick

    async def stream_bars(self, symbols: Sequence[str]) -> AsyncIterator[Bar]:
        wanted = set(symbols)
        for bar in self._bars:
            if bar.symbol in wanted:
                yield bar


class AdapterFeed(DataFeed):
    """A live ``DataFeed`` backed by a ``BrokerAdapter``'s tick stream.

    Lets the ``PaperBroker`` price simulated fills against a **real venue's live
    ticks** (paper-crypto): the wrapped adapter provides market data only — orders
    are simulated locally by the paper broker and never sent to the venue, so a
    read-only key is sufficient. Bars are not streamed here (the loop builds them
    from ticks via ``BarBuilder``).
    """

    def __init__(self, adapter: BrokerAdapter) -> None:
        self._adapter = adapter

    def stream_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        return self._adapter.subscribe_ticks(symbols)

    def stream_bars(self, symbols: Sequence[str]) -> AsyncIterator[Bar]:
        raise NotImplementedError("AdapterFeed streams ticks only; bars are built by BarBuilder")


class TeeFeed(DataFeed):
    """Yield the inner feed's ticks, teeing each into a callback first — the paper
    assembly's market-data splice (M4.5): the same tick that drives the BarBuilder
    also updates the ``PaperBroker``'s quotes, so simulated fills price off exactly
    the stream the strategy saw (live feed + simulated execution, R9 — not replay)."""

    def __init__(self, inner: DataFeed, on_tick: Callable[[Tick], None]) -> None:
        self._inner = inner
        self._on_tick = on_tick

    async def stream_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        async for tick in self._inner.stream_ticks(symbols):
            self._on_tick(tick)
            yield tick

    def stream_bars(self, symbols: Sequence[str]) -> AsyncIterator[Bar]:
        raise NotImplementedError("TeeFeed streams ticks only; bars are built by BarBuilder")
