"""Shared portable performance metrics (pure-stdlib) for the rigor kernel.

A single home for the small statistical primitives the rigor modules share. ``sharpe`` was
triplicated across ``backtest.overfitting`` (the CSCV selection metric), ``research.calibration``,
and ``research.quant_analyst`` — a drift risk for a number the gate depends on — so it lives here in
exactly one place. Pure-stdlib (``statistics``), so the lean worker never pulls numpy/scipy.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence


def sharpe(returns: Sequence[float]) -> float:
    """Per-series Sharpe ratio: mean / population stdev. Returns ``0.0`` with fewer than two points
    or no dispersion — an undefined ratio is treated as no edge, never an error or a div-by-zero."""
    if len(returns) < 2:
        return 0.0
    sd = statistics.pstdev(returns)
    return statistics.fmean(returns) / sd if sd > 0 else 0.0
