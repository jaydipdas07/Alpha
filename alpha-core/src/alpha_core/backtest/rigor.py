"""Backtest rigor (Phase 7 B7.3/B7.4) — the checks that keep results honest.

- ``audit_no_lookahead`` — an automated look-ahead audit: perturbing future bars
  must not change any *past* signal. A correct streaming strategy passes; one that
  peeks at the future is caught.
- ``walk_forward`` — run the strategy over sequential windows (out-of-sample).
- ``stress_gate`` — re-run at 2x slippage (ADR 0008); the edge must survive.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from alpha_core.backtest.runner import BacktestResult, run_backtest, run_portfolio_backtest
from alpha_core.core.enums import Venue
from alpha_core.core.interfaces import PortfolioConstructor, Strategy
from alpha_core.core.models import Bar
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.helpers.config import PortfolioConfig
from alpha_core.risk.limits import RiskConfig
from alpha_core.strategy.engine import StrategyEngine

MakeStrategy = Callable[[list[Bar]], Strategy]
MakeStrategies = Callable[[list[Bar]], list[Strategy]]
MakeAllocator = Callable[[], PortfolioConstructor]


@dataclass(frozen=True, slots=True)
class LookAheadReport:
    passed: bool
    detail: str = ""


def _signal_keys_by_bar(strategy: Strategy, bars: list[Bar]) -> list[list[tuple[str, ...]]]:
    engine = StrategyEngine(strategy)
    return [
        [
            (s.symbol, s.side.value, str(s.quantity), s.order_type.value)
            for s in engine.process_bar(b)
        ]
        for b in bars
    ]


def _with_close(bar: Bar, new_close: Decimal) -> Bar:
    return bar.model_copy(
        update={
            "close": new_close,
            "high": max(bar.open, new_close) + Decimal("1"),
            "low": max(Decimal("0.01"), min(bar.open, new_close) - Decimal("1")),
        }
    )


def _perturb_from(
    bars: list[Bar], pivot: int, new_close: Callable[[Decimal], Decimal]
) -> list[Bar]:
    """Perturb every bar from ``pivot`` onward (the whole future tail), leaving the
    bars before ``pivot`` untouched."""
    return [*bars[:pivot], *(_with_close(b, new_close(b.close)) for b in bars[pivot:])]


def _pivot_indices(n: int, max_pivots: int) -> list[int]:
    """Future-tail boundaries in ``[1, n)`` to test: all of them when there are
    few, else ``max_pivots`` evenly-spaced ones. Always includes the last bar
    (``n-1``) so the final-bar / global-statistic case stays covered."""
    if n - 1 <= max_pivots:
        return list(range(1, n))
    step = (n - 1) / max_pivots
    sampled = {min(n - 1, max(1, round(1 + k * step))) for k in range(max_pivots)}
    sampled.add(n - 1)
    return sorted(sampled)


def audit_no_lookahead(
    make_strategy: MakeStrategy, bars: list[Bar], *, max_pivots: int = 24
) -> LookAheadReport:
    """Sweep a pivot across the series; for each pivot perturb the *entire future
    tail* (both far up and far down) and require every signal *before* the pivot to
    be unchanged — i.e. nothing before the pivot may depend on anything at or after
    it.

    ``make_strategy(bars)`` builds a fresh strategy for the series it will run on
    (so a strategy that peeked at those bars is exercised against the perturbed
    future). The earlier audit perturbed only the final bar, which catches
    look-ahead onto the last bar or a global statistic but misses a strategy that
    peeks at an *interior* future bar; sweeping the pivot closes that gap. Pivots
    are sampled evenly (capped at ``max_pivots``) so the audit stays tractable on
    long series — it is a strong necessary check, not an exhaustive proof. Returns
    ``passed=False`` at the first offending bar.
    """
    if len(bars) < 2:
        return LookAheadReport(True, "too few bars to audit")
    base = _signal_keys_by_bar(make_strategy(bars), bars)
    directions: tuple[Callable[[Decimal], Decimal], ...] = (
        lambda c: c * Decimal("2") + Decimal("1"),
        lambda c: c / Decimal("2"),
    )
    for pivot in _pivot_indices(len(bars), max_pivots):
        for new_close in directions:
            pbars = _perturb_from(bars, pivot, new_close)
            perturbed = _signal_keys_by_bar(make_strategy(pbars), pbars)
            for i in range(pivot):  # every bar strictly before the perturbed tail
                if base[i] != perturbed[i]:
                    return LookAheadReport(
                        False,
                        f"look-ahead: signals at bar {i} changed when only bars >= {pivot} changed",
                    )
    return LookAheadReport(True)


async def walk_forward(
    *,
    bars: list[Bar],
    make_strategy: MakeStrategy,
    instruments: dict[str, InstrumentMeta],
    risk_config: RiskConfig,
    cost_config: dict[str, object],
    n_windows: int = 3,
    venue: Venue = Venue.NSE,
    starting_cash: Decimal = Decimal("1000000"),
) -> list[BacktestResult]:
    """Backtest over ``n_windows`` sequential out-of-sample windows."""
    if n_windows < 1:
        raise ValueError("n_windows must be >= 1")
    size = max(1, len(bars) // n_windows)
    results: list[BacktestResult] = []
    for w in range(n_windows):
        window = bars[w * size : (w + 1) * size]
        if not window:
            break
        results.append(
            await run_backtest(
                bars=window,
                strategy=make_strategy(window),
                instruments=instruments,
                risk_config=risk_config,
                cost_config=cost_config,
                venue=venue,
                starting_cash=starting_cash,
            )
        )
    return results


@dataclass(frozen=True, slots=True)
class StressGateResult:
    base: BacktestResult
    stressed: BacktestResult
    survived: bool  # edge still positive under 2x slippage


@dataclass(frozen=True, slots=True)
class RigorReport:
    """The combined backtest-rigor verdict (P13.10)."""

    no_lookahead: bool
    lookahead_detail: str
    stress: StressGateResult
    walk_forward: list[BacktestResult]

    @property
    def structurally_sound(self) -> bool:
        """No look-ahead — a hard requirement for *any* strategy (a bug if false)."""
        return self.no_lookahead

    @property
    def walk_forward_stable(self) -> bool:
        """Every out-of-sample window is non-negative (no single-window mirage)."""
        return bool(self.walk_forward) and all(r.stats.final_pnl >= 0 for r in self.walk_forward)

    @property
    def edge_robust(self) -> bool:
        """The honest single-strategy edge gate: look-ahead clean AND survives 2x
        slippage (ADR 0008) AND stable across out-of-sample windows. Mirrors the
        cross-instrument gate (``PortfolioRigorReport.edge_robust``) so the single-
        and multi-strategy paths enforce the same contract."""
        return self.no_lookahead and self.stress.survived and self.walk_forward_stable


async def rigor_report(
    *,
    bars: list[Bar],
    make_strategy: MakeStrategy,
    instruments: dict[str, InstrumentMeta],
    risk_config: RiskConfig,
    cost_config: dict[str, object],
    n_windows: int = 3,
    venue: Venue = Venue.NSE,
    starting_cash: Decimal = Decimal("1000000"),
) -> RigorReport:
    """Run the full rigor suite — look-ahead audit + 2x stress + walk-forward —
    and return one verdict (P13.10)."""
    audit = audit_no_lookahead(make_strategy, bars)
    stress = await stress_gate(
        bars=bars,
        make_strategy=make_strategy,
        instruments=instruments,
        risk_config=risk_config,
        cost_config=cost_config,
        venue=venue,
        starting_cash=starting_cash,
    )
    windows = await walk_forward(
        bars=bars,
        make_strategy=make_strategy,
        instruments=instruments,
        risk_config=risk_config,
        cost_config=cost_config,
        n_windows=n_windows,
        venue=venue,
        starting_cash=starting_cash,
    )
    return RigorReport(audit.passed, audit.detail, stress, windows)


async def stress_gate(
    *,
    bars: list[Bar],
    make_strategy: MakeStrategy,
    instruments: dict[str, InstrumentMeta],
    risk_config: RiskConfig,
    cost_config: dict[str, object],
    venue: Venue = Venue.NSE,
    starting_cash: Decimal = Decimal("1000000"),
) -> StressGateResult:
    """Run base + 2x slippage; ``survived`` iff the edge stays positive (ADR 0008)."""
    base = await run_backtest(
        bars=bars,
        strategy=make_strategy(bars),
        instruments=instruments,
        risk_config=risk_config,
        cost_config=cost_config,
        venue=venue,
        starting_cash=starting_cash,
        stress=False,
    )
    stressed = await run_backtest(
        bars=bars,
        strategy=make_strategy(bars),
        instruments=instruments,
        risk_config=risk_config,
        cost_config=cost_config,
        venue=venue,
        starting_cash=starting_cash,
        stress=True,
    )
    return StressGateResult(base=base, stressed=stressed, survived=stressed.stats.final_pnl > 0)


# --- Cross-instrument portfolio rigor (R6) -------------------------------------


def _bars_by_symbol(bars: list[Bar]) -> dict[str, list[Bar]]:
    out: dict[str, list[Bar]] = {}
    for bar in bars:
        out.setdefault(bar.symbol, []).append(bar)
    return out


def audit_no_lookahead_portfolio(
    make_strategies: MakeStrategies, bars: list[Bar]
) -> LookAheadReport:
    """Cross-sectional look-ahead audit (R6): each strategy, on each instrument's
    own bar stream, must not change a past signal when only the future changes.
    Reuses the single-series audit per (strategy, instrument) — strategies are
    per-symbol streaming, so this fully covers the cross-section."""
    by_symbol = _bars_by_symbol(bars)
    for idx in range(len(make_strategies(bars))):

        def maker(window: list[Bar], _i: int = idx) -> Strategy:
            return make_strategies(window)[_i]

        for symbol, series in by_symbol.items():
            report = audit_no_lookahead(maker, series)
            if not report.passed:
                return LookAheadReport(False, f"strategy[{idx}] on {symbol}: {report.detail}")
    return LookAheadReport(True)


async def portfolio_stress_gate(
    *,
    bars: list[Bar],
    make_strategies: MakeStrategies,
    make_allocator: MakeAllocator,
    portfolio_config: PortfolioConfig,
    instruments: dict[str, InstrumentMeta],
    risk_config: RiskConfig,
    cost_config: dict[str, object],
    venue: Venue = Venue.NSE,
    starting_cash: Decimal = Decimal("1000000"),
) -> StressGateResult:
    """Portfolio backtest at base vs 2x slippage; ``survived`` iff still positive."""
    base = await run_portfolio_backtest(
        bars=bars,
        strategies=make_strategies(bars),
        allocator=make_allocator(),
        portfolio_config=portfolio_config,
        instruments=instruments,
        risk_config=risk_config,
        cost_config=cost_config,
        venue=venue,
        starting_cash=starting_cash,
        stress=False,
    )
    stressed = await run_portfolio_backtest(
        bars=bars,
        strategies=make_strategies(bars),
        allocator=make_allocator(),
        portfolio_config=portfolio_config,
        instruments=instruments,
        risk_config=risk_config,
        cost_config=cost_config,
        venue=venue,
        starting_cash=starting_cash,
        stress=True,
    )
    return StressGateResult(base=base, stressed=stressed, survived=stressed.stats.final_pnl > 0)


async def portfolio_walk_forward(
    *,
    bars: list[Bar],
    make_strategies: MakeStrategies,
    make_allocator: MakeAllocator,
    portfolio_config: PortfolioConfig,
    instruments: dict[str, InstrumentMeta],
    risk_config: RiskConfig,
    cost_config: dict[str, object],
    n_windows: int = 3,
    venue: Venue = Venue.NSE,
    starting_cash: Decimal = Decimal("1000000"),
) -> list[BacktestResult]:
    """Portfolio backtest over ``n_windows`` sequential out-of-sample windows,
    split by **time** (so every instrument is present in each window)."""
    if n_windows < 1:
        raise ValueError("n_windows must be >= 1")
    timestamps = sorted({bar.start for bar in bars})
    if not timestamps:
        return []
    size = max(1, len(timestamps) // n_windows)
    results: list[BacktestResult] = []
    for w in range(n_windows):
        window_ts = set(timestamps[w * size : (w + 1) * size])
        if not window_ts:
            break
        window = [bar for bar in bars if bar.start in window_ts]
        results.append(
            await run_portfolio_backtest(
                bars=window,
                strategies=make_strategies(window),
                allocator=make_allocator(),
                portfolio_config=portfolio_config,
                instruments=instruments,
                risk_config=risk_config,
                cost_config=cost_config,
                venue=venue,
                starting_cash=starting_cash,
            )
        )
    return results


@dataclass(frozen=True, slots=True)
class PortfolioRigorReport:
    """The combined cross-instrument rigor verdict (R6)."""

    no_lookahead: bool
    lookahead_detail: str
    stress: StressGateResult
    walk_forward: list[BacktestResult]

    @property
    def structurally_sound(self) -> bool:
        """No look-ahead anywhere in the cross-section (a bug if false)."""
        return self.no_lookahead

    @property
    def walk_forward_stable(self) -> bool:
        """Every out-of-sample window is non-negative (no single-window mirage)."""
        return bool(self.walk_forward) and all(r.stats.final_pnl >= 0 for r in self.walk_forward)

    @property
    def turnover_ratio(self) -> float:
        """Gross traded value / capital (churn — a capacity & cost-drag signal)."""
        return self.stress.base.stats.turnover_ratio

    @property
    def edge_robust(self) -> bool:
        """The honest cross-instrument edge gate: look-ahead clean AND survives 2x
        slippage AND stable across out-of-sample windows (R6, ``--require-edge``)."""
        return self.no_lookahead and self.stress.survived and self.walk_forward_stable


async def portfolio_rigor_report(
    *,
    bars: list[Bar],
    make_strategies: MakeStrategies,
    make_allocator: MakeAllocator,
    portfolio_config: PortfolioConfig,
    instruments: dict[str, InstrumentMeta],
    risk_config: RiskConfig,
    cost_config: dict[str, object],
    n_windows: int = 3,
    venue: Venue = Venue.NSE,
    starting_cash: Decimal = Decimal("1000000"),
) -> PortfolioRigorReport:
    """Run the full cross-instrument rigor suite (R6): cross-sectional look-ahead
    audit + 2x stress + cross-instrument walk-forward → one honest verdict."""
    audit = audit_no_lookahead_portfolio(make_strategies, bars)
    stress = await portfolio_stress_gate(
        bars=bars,
        make_strategies=make_strategies,
        make_allocator=make_allocator,
        portfolio_config=portfolio_config,
        instruments=instruments,
        risk_config=risk_config,
        cost_config=cost_config,
        venue=venue,
        starting_cash=starting_cash,
    )
    windows = await portfolio_walk_forward(
        bars=bars,
        make_strategies=make_strategies,
        make_allocator=make_allocator,
        portfolio_config=portfolio_config,
        instruments=instruments,
        risk_config=risk_config,
        cost_config=cost_config,
        n_windows=n_windows,
        venue=venue,
        starting_cash=starting_cash,
    )
    return PortfolioRigorReport(audit.passed, audit.detail, stress, windows)
