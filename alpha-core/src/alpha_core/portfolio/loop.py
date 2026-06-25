"""Bar-close barrier + the shared rebalance pass (F4).

The portfolio engine allocates at **bar-close boundaries** over the cross-section
of the universe (scaling design PA). Two pieces, used by BOTH the backtester and
the live loop so they execute identically (ADR 0001):

- ``BarBarrier`` — buffers closed bars by their boundary timestamp and releases the
  cross-section when the boundary advances (a strictly-later bar arrives). A *late*
  bar for an already-released boundary (a thin symbol that printed after a liquid one
  advanced the boundary) is **carried into the next cross-section**, not dropped — so
  it rebalances one boundary late instead of being silently excluded (M4).
- ``rebalance_pass`` — the one serialized step: run every strategy over the
  cross-section, fold events into the ``SignalBook``, build target weights with the
  allocator, diff current→target into orders, and submit them through the OMS (the
  single audited authority). Identical offline and live.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from alpha_core.core.enums import AssetClass, OrderType
from alpha_core.core.interfaces import PortfolioConstructor, Strategy
from alpha_core.core.models import Bar, Signal
from alpha_core.execution.oms import OMS
from alpha_core.helpers.config import PortfolioConfig
from alpha_core.portfolio.rebalance import rebalance_orders
from alpha_core.portfolio.risk_overlay import PortfolioRiskOverlay
from alpha_core.portfolio.signal_book import SignalBook
from alpha_core.scheduler.clock import Clock
from alpha_core.strategy.engine import StrategyEngine


class BarBarrier:
    """Collects closed bars into per-boundary cross-sections (bar-aligned)."""

    def __init__(self) -> None:
        self._boundary: datetime | None = None
        self._buffer: list[Bar] = []

    def add(self, bar: Bar) -> list[Bar] | None:
        """Buffer ``bar``; return the completed cross-section when the boundary
        advances (a strictly-later bar arrives), else ``None``."""
        if self._boundary is None or bar.start == self._boundary:
            self._boundary = bar.start
            self._buffer.append(bar)
            return None
        if bar.start > self._boundary:
            completed = self._buffer
            self._boundary = bar.start
            self._buffer = [bar]
            return completed
        # A LATE bar for an already-released boundary: a thinly-traded symbol whose bar
        # printed only after a more-liquid symbol advanced the boundary (per-symbol
        # tick→bar aggregation emits a symbol's bar only on its next-bucket tick). Don't
        # DROP it — that silently excludes the symbol from the rebalance and diverges
        # from the backtest (which sorts all bars, so every symbol is grouped). Carry it
        # into the current buffer so it's processed at the next release (one boundary
        # late); a symbol's bars always emit in time order, so per-symbol order holds.
        # The offline backtest feeds sorted bars and never hits this path, so it's
        # unchanged (M4).
        self._buffer.append(bar)
        return None

    def flush(self) -> list[Bar]:
        """Release the final buffered boundary (end of stream / shutdown)."""
        out = self._buffer
        self._buffer = []
        self._boundary = None
        return out


async def rebalance_pass(
    *,
    oms: OMS,
    engines: Sequence[StrategyEngine],
    book: SignalBook,
    allocator: PortfolioConstructor,
    portfolio_config: PortfolioConfig,
    cross_section: Sequence[Bar],
    prices: dict[str, Decimal],
    asset_class: dict[str, AssetClass],
    capital: Decimal,
    now: datetime,
    overlay: PortfolioRiskOverlay | None = None,
) -> None:
    """One bar-close rebalance: cross-section → signals → targets → diff → orders.

    Mutates ``prices``/``asset_class`` from the cross-section (the caller keeps
    them across bars so held names that didn't print this bar still price/exit).
    The optional ``overlay`` clamps the targets to the portfolio-risk caps before
    they become orders (P3). Orders go through the OMS, the serialized audited authority.
    """
    signals: list[Signal] = []
    for bar in cross_section:
        prices[bar.symbol] = bar.close
        asset_class[bar.symbol] = bar.asset_class
        for engine in engines:
            signals.extend(engine.process_bar(bar))
    book.update(signals)
    targets = allocator.construct(book.active(), oms.positions, capital)
    if overlay is not None:
        targets = overlay.apply(targets)
    current_qty = {p.symbol: p.quantity for p in oms.positions}
    for order in rebalance_orders(targets, current_qty, prices, capital, portfolio_config):
        await oms.submit_signal(
            Signal(
                strategy_id="portfolio",
                symbol=order.symbol,
                asset_class=asset_class[order.symbol],
                side=order.side,
                quantity=order.quantity,
                order_type=OrderType.MARKET,
                created_at=now,
            ),
            reference_price=prices[order.symbol],
        )


class PortfolioBarHandler:
    """Stateful per-bar driver for the live portfolio loop (F4).

    Holds the standing ``SignalBook`` + ``BarBarrier`` across bars; ``handle`` feeds
    each closed bar to the barrier and runs ``rebalance_pass`` when a cross-section
    completes. The live loop calls ``handle`` per closed bar — the same pass the
    backtester runs offline, so live and backtest agree (ADR 0001).
    """

    def __init__(
        self,
        *,
        oms: OMS,
        strategies: Sequence[Strategy],
        allocator: PortfolioConstructor,
        portfolio_config: PortfolioConfig,
        capital: Decimal,
        clock: Clock,
        overlay: PortfolioRiskOverlay | None = None,
    ) -> None:
        self._oms = oms
        self._engines = [StrategyEngine(s) for s in strategies]
        self._book = SignalBook()
        self._barrier = BarBarrier()
        self._allocator = allocator
        self._config = portfolio_config
        self._capital = capital
        self._clock = clock
        self._overlay = overlay
        self._prices: dict[str, Decimal] = {}
        self._asset_class: dict[str, AssetClass] = {}

    async def handle(self, bar: Bar) -> None:
        """Buffer ``bar``; rebalance when its bar-close boundary completes."""
        released = self._barrier.add(bar)
        if released is None:
            return
        await rebalance_pass(
            oms=self._oms,
            engines=self._engines,
            book=self._book,
            allocator=self._allocator,
            portfolio_config=self._config,
            cross_section=released,
            prices=self._prices,
            asset_class=self._asset_class,
            capital=self._capital,
            now=self._clock.now(),
            overlay=self._overlay,
        )
        self._oms.mark(dict(self._prices))
