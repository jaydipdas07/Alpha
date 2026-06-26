"""vectorbt coarse pre-screen (R7, B1a.8) — cull obvious losers cheaply, upstream of CPCV.

The discovery loop generates far more candidates than CPCV/PBO can afford to run fully. This
runs a candidate's **actual** signals (from the event-driven ``StrategyEngine``, so the screen
is faithful to the strategy) through **vectorbt**'s vectorized portfolio sim over a coarse
out-of-sample walk-forward, and culls anything whose median per-bar Sharpe is below a floor —
so the expensive CPCV only runs on survivors.

Deliberately **coarse**: float (not Decimal), no full cost model or risk gate — it is a cheap
filter, not the gate (CPCV/PBO + DSR are). ``vectorbt``/``numpy`` are research-extra deps (dev
group) — the worker never imports this module, so the lean kernel stays vectorbt-free.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import vectorbt as vbt

from alpha_core.core.enums import Side
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar
from alpha_core.strategy.engine import StrategyEngine

MakeStrategy = Callable[[list[Bar]], Strategy]


@dataclass(frozen=True, slots=True)
class PrescreenResult:
    """The coarse pre-screen verdict."""

    passed: bool  # survived the screen -> proceeds to the full CPCV gate
    median_sharpe: float  # median per-bar Sharpe across the OOS windows
    window_sharpes: tuple[float, ...]


def _signal_arrays(make_strategy: MakeStrategy, bars: list[Bar]) -> tuple[np.ndarray, np.ndarray]:
    """The strategy's signals over ``bars`` as vectorbt long/short boolean arrays: a BUY is a
    long signal, a SELL a short signal. With ``direction="both"`` (a coarse flip model of
    directional alpha) the screen credits short edge too — vital on a short-capable perp
    venue, where culling a short winner would be the costly error."""
    engine = StrategyEngine(make_strategy(bars))
    entries = np.zeros(len(bars), dtype=bool)
    exits = np.zeros(len(bars), dtype=bool)
    for i, bar in enumerate(bars):
        for signal in engine.process_bar(bar):
            if signal.side is Side.BUY:
                entries[i] = True
            if signal.side is Side.SELL:
                exits[i] = True
    return entries, exits


def _coarse_sharpe(close: np.ndarray, entries: np.ndarray, exits: np.ndarray) -> float:
    """Per-bar Sharpe of vectorbt's fast portfolio sim of these signals (0 if undefined).
    ``direction="both"`` lets a SELL open a short (not just exit a long), so short alpha is
    screened too. The ``freq`` only satisfies vectorbt; the metric is computed from the raw
    per-bar returns, so it is annualization-independent."""
    pf = vbt.Portfolio.from_signals(close, entries, exits, freq="1D", direction="both")
    returns = np.asarray(pf.returns(), dtype=float)
    returns = returns[~np.isnan(returns)]
    if returns.size < 2:  # pragma: no cover - defensive: callers guard windows >= 2 bars
        return 0.0
    sd = float(returns.std())
    return float(returns.mean() / sd) if sd > 0.0 else 0.0


def prescreen(
    make_strategy: MakeStrategy, bars: list[Bar], *, min_sharpe: float, n_windows: int
) -> PrescreenResult:
    """Coarse vectorbt walk-forward pre-screen: split ``bars`` into ``n_windows`` sequential
    out-of-sample windows, take each window's coarse per-bar Sharpe, and **pass** iff their
    median clears ``min_sharpe`` (else the candidate is culled before CPCV)."""
    if n_windows < 1:
        raise ValueError(f"n_windows must be >= 1; got {n_windows}")
    size = max(1, len(bars) // n_windows)
    sharpes: list[float] = []
    for w in range(n_windows):
        window = bars[w * size : (w + 1) * size]
        if len(window) < 2:
            break
        entries, exits = _signal_arrays(make_strategy, window)
        close = np.array([float(b.close) for b in window], dtype=float)
        sharpes.append(_coarse_sharpe(close, entries, exits))
    median = statistics.median(sharpes) if sharpes else 0.0
    return PrescreenResult(
        passed=bool(sharpes) and median >= min_sharpe,
        median_sharpe=median,
        window_sharpes=tuple(sharpes),
    )
