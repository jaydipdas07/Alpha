"""Event-driven backtester (Phase 7) — the SAME pipeline as paper/live.

Reuses ``StrategyEngine`` -> OMS (risk -> FSM -> ``PaperBroker`` -> ``StateStore``)
-> fills, the §3 ``CostModel``, and the shared session helpers, so a backtest and
a live run share the strategy and execution path (ADR 0001). Records a per-bar
mark-to-market equity curve and computes a stats report. The 2x slippage stress
test (ADR 0008) is the ``stress`` flag.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from alpha_core.adapters.paper import PaperBroker
from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.interfaces import BrokerEventKind, PortfolioConstructor, Strategy
from alpha_core.core.models import Bar
from alpha_core.execution.costs import CostModel, InstrumentMeta
from alpha_core.execution.oms import OMS
from alpha_core.execution.session import handle_kill, quote_from_bar, square_off
from alpha_core.execution.state import PnlLedgerRow, StateStore
from alpha_core.helpers.config import PortfolioConfig
from alpha_core.portfolio.loop import BarBarrier, rebalance_pass
from alpha_core.portfolio.risk_overlay import PortfolioRiskOverlay
from alpha_core.portfolio.signal_book import SignalBook
from alpha_core.risk.limits import RiskConfig
from alpha_core.risk.manager import RiskManager
from alpha_core.scheduler.clock import FakeClock
from alpha_core.strategy.engine import StrategyEngine


@dataclass(frozen=True, slots=True)
class BacktestStats:
    final_pnl: Decimal
    total_return_pct: float
    max_drawdown_pct: float
    sharpe: float  # per-bar (not annualized)
    num_fills: int
    total_fees: Decimal
    win_rate: float
    traded_notional: Decimal = Decimal(0)  # sum |fill price x qty| - gross traded value
    turnover_ratio: float = 0.0  # traded_notional / capital (churn; capacity input, R6)


@dataclass(frozen=True, slots=True)
class BacktestResult:
    stats: BacktestStats
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    halted: bool = False

    def render(self) -> str:
        s = self.stats
        return "\n".join(
            [
                "=== Vega backtest — stats ===",
                f"final P&L:      {s.final_pnl}",
                f"total return:   {s.total_return_pct:.4f}%",
                f"max drawdown:   {s.max_drawdown_pct:.4f}%",
                f"Sharpe (bar):   {s.sharpe:.4f}",
                f"fills:          {s.num_fills}",
                f"total fees:     {s.total_fees}",
                f"win rate:       {s.win_rate:.4f}",
                f"traded notional:{s.traded_notional}",
                f"turnover:       {s.turnover_ratio:.2f}x",
                f"halted:         {self.halted}",
            ]
        )


def _compute_stats(
    *,
    starting_cash: Decimal,
    equity_curve: list[tuple[datetime, Decimal]],
    num_fills: int,
    total_fees: Decimal,
    realized_events: list[Decimal],
    traded_notional: Decimal = Decimal(0),
) -> BacktestStats:
    final_pnl = equity_curve[-1][1] if equity_curve else Decimal(0)
    equity = [starting_cash + pnl for _, pnl in equity_curve] or [starting_cash]

    peak = equity[0]
    max_dd = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, float((peak - value) / peak))

    returns = [
        float((equity[i] - equity[i - 1]) / equity[i - 1])
        for i in range(1, len(equity))
        if equity[i - 1] != 0
    ]
    sharpe = 0.0
    if len(returns) > 1:
        sd = statistics.pstdev(returns)
        sharpe = statistics.mean(returns) / sd if sd > 0 else 0.0

    wins = sum(1 for r in realized_events if r > 0)
    win_rate = wins / len(realized_events) if realized_events else 0.0

    return BacktestStats(
        final_pnl=final_pnl,
        total_return_pct=float(final_pnl / starting_cash * 100) if starting_cash else 0.0,
        max_drawdown_pct=max_dd * 100,
        sharpe=sharpe,
        num_fills=num_fills,
        total_fees=total_fees,
        win_rate=win_rate,
        traded_notional=traded_notional,
        turnover_ratio=float(traded_notional / starting_cash) if starting_cash else 0.0,
    )


async def run_backtest(
    *,
    bars: list[Bar],
    strategy: Strategy,
    instruments: dict[str, InstrumentMeta],
    risk_config: RiskConfig,
    cost_config: dict[str, object],
    venue: Venue = Venue.NSE,
    starting_cash: Decimal = Decimal("1000000"),
    stress: bool = False,
) -> BacktestResult:
    """Run ``strategy`` over historical ``bars`` and return stats."""
    broker = PaperBroker(
        cost_model=CostModel(cost_config),
        instruments=instruments,
        starting_cash=starting_cash,
        stress=stress,
    )
    risk = RiskManager(risk_config)
    store = StateStore("sqlite:///:memory:")
    store.create_schema()
    # Drive OMS time from bar time (not wall-clock) so the order throttle + FSM
    # stamps are deterministic and match a live run on the same bars (G23).
    clock = FakeClock(bars[0].start) if bars else FakeClock(datetime(2000, 1, 1, tzinfo=UTC))
    oms = OMS(adapter=broker, risk=risk, store=store, venue=venue, clock=clock)
    engine = StrategyEngine(strategy)

    equity_curve: list[tuple[datetime, Decimal]] = []
    for bar in bars:
        clock.set(bar.start + bar.interval)  # decision instant = bar close
        broker.on_tick(quote_from_bar(bar))
        for signal in engine.process_bar(bar):
            await oms.submit_signal(signal, reference_price=bar.close)
        await oms.drain_events()
        oms.mark({bar.symbol: bar.close})
        total = oms.total_realized_pnl() + oms.total_unrealized_pnl()
        equity_curve.append((bar.start + bar.interval, total))
        if risk.is_halted:
            # A kill mid-backtest flattens (cancel + flatten) so the curve is
            # realistic — square_off would be blocked while halted.
            await handle_kill(oms, risk.halt_trigger)
            break

    if not risk.is_halted:
        await square_off(oms)
    if bars:
        equity_curve.append((bars[-1].start + bars[-1].interval, oms.total_realized_pnl()))

    num_fills = len([e for e in broker.emitted if e.kind is BrokerEventKind.FILL])
    total_fees = sum(
        (e.fill.fees or Decimal(0) for e in broker.emitted if e.fill is not None),
        Decimal(0),
    )
    traded_notional = sum(
        (e.fill.price * e.fill.quantity for e in broker.emitted if e.fill is not None),
        Decimal(0),
    )
    with store.transaction() as s:
        realized_events = list(s.execute(select(PnlLedgerRow.realized_pnl)).scalars())

    stats = _compute_stats(
        starting_cash=starting_cash,
        equity_curve=equity_curve,
        num_fills=num_fills,
        total_fees=total_fees,
        realized_events=realized_events,
        traded_notional=traded_notional,
    )
    store.dispose()
    return BacktestResult(stats=stats, equity_curve=equity_curve, halted=risk.is_halted)


async def run_portfolio_backtest(
    *,
    bars: list[Bar],
    strategies: Sequence[Strategy],
    allocator: PortfolioConstructor,
    portfolio_config: PortfolioConfig,
    instruments: dict[str, InstrumentMeta],
    risk_config: RiskConfig,
    cost_config: dict[str, object],
    venue: Venue = Venue.NSE,
    starting_cash: Decimal = Decimal("1000000"),
    stress: bool = False,
    overlay: PortfolioRiskOverlay | None = None,
) -> BacktestResult:
    """Multi-instrument, multi-strategy backtest through the pure allocator (R5).

    Drives the SAME bar-close barrier + rebalance pass as the live loop (F4): the
    ``BarBarrier`` groups bars into cross-sections; each is run through every
    strategy → ``SignalBook`` → allocator → ``rebalance_orders`` → OMS. The
    Universe→Alpha→Portfolio→Risk→Execution stages all run through the SAME
    OMS/risk/cost path as live (ADR 0001), so a backtest and a live run agree.
    """
    broker = PaperBroker(
        cost_model=CostModel(cost_config),
        instruments=instruments,
        starting_cash=starting_cash,
        stress=stress,
    )
    risk = RiskManager(risk_config)
    store = StateStore("sqlite:///:memory:")
    store.create_schema()
    start = min((b.start for b in bars), default=datetime(2000, 1, 1, tzinfo=UTC))
    clock = FakeClock(start)
    oms = OMS(adapter=broker, risk=risk, store=store, venue=venue, clock=clock)
    engines = [StrategyEngine(s) for s in strategies]
    book = SignalBook()

    prices: dict[str, Decimal] = {}
    asset_class: dict[str, AssetClass] = {}
    equity_curve: list[tuple[datetime, Decimal]] = []

    # Sorted offline bars through the barrier reproduce the live cross-sections.
    barrier = BarBarrier()
    sections: list[list[Bar]] = []
    for bar in sorted(bars, key=lambda b: (b.start, b.symbol)):
        released = barrier.add(bar)
        if released is not None:
            sections.append(released)
    final = barrier.flush()
    if final:
        sections.append(final)

    for cross_section in sections:
        close_ts = cross_section[0].start + cross_section[0].interval
        clock.set(close_ts)
        for bar in cross_section:
            broker.on_tick(quote_from_bar(bar))
        await rebalance_pass(
            oms=oms,
            engines=engines,
            book=book,
            allocator=allocator,
            portfolio_config=portfolio_config,
            cross_section=cross_section,
            prices=prices,
            asset_class=asset_class,
            capital=starting_cash,
            now=close_ts,
            overlay=overlay,
        )
        await oms.drain_events()
        oms.mark(dict(prices))
        equity_curve.append((close_ts, oms.total_realized_pnl() + oms.total_unrealized_pnl()))
        if risk.is_halted:
            await handle_kill(oms, risk.halt_trigger)
            break

    if not risk.is_halted:
        await square_off(oms)
    if equity_curve:
        equity_curve.append((equity_curve[-1][0], oms.total_realized_pnl()))

    num_fills = len([e for e in broker.emitted if e.kind is BrokerEventKind.FILL])
    total_fees = sum(
        (e.fill.fees or Decimal(0) for e in broker.emitted if e.fill is not None),
        Decimal(0),
    )
    traded_notional = sum(
        (e.fill.price * e.fill.quantity for e in broker.emitted if e.fill is not None),
        Decimal(0),
    )
    with store.transaction() as s:
        realized_events = list(s.execute(select(PnlLedgerRow.realized_pnl)).scalars())
    stats = _compute_stats(
        starting_cash=starting_cash,
        equity_curve=equity_curve,
        num_fills=num_fills,
        total_fees=total_fees,
        realized_events=realized_events,
        traded_notional=traded_notional,
    )
    store.dispose()
    return BacktestResult(stats=stats, equity_curve=equity_curve, halted=risk.is_halted)
