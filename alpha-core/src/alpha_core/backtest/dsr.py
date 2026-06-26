"""Deflated / Probabilistic Sharpe Ratio (B1a.5) — selection-bias-corrected significance.

Bailey & López de Prado (2014), "The Deflated Sharpe Ratio". Pure-stdlib
(``statistics.NormalDist`` for Φ and Φ⁻¹), so it stays importable in the lean worker
kernel and on the research box alike.

- ``probabilistic_sharpe_ratio`` — P(true Sharpe > benchmark) for an observed return
  series, correcting the Sharpe's standard error for sample length, skewness and
  kurtosis (a skewed or fat-tailed series needs a higher observed Sharpe to clear a bar).
- ``expected_max_sharpe`` — SR₀, the Sharpe you'd expect to see *by chance* as the best
  of ``n_trials`` independent trials (the multiple-testing penalty); it grows with the
  trial count supplied by the keyed trial ledger (R4).
- ``deflated_sharpe_ratio`` — the PSR measured against that SR₀ benchmark: the
  probability the edge is real *after* deflating for how many trials were run. The trial
  count is per-cell ``(market, family, window)``, so the penalty is invariant to
  unrelated search cells.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist, fmean

_EULER_MASCHERONI = 0.5772156649015329  # gamma — for the expected-maximum order statistic
_NORMAL = NormalDist()


def _return_stats(returns: Sequence[float]) -> tuple[float, float, float, int]:
    """``(observed Sharpe, skewness, kurtosis, n)`` from population moments; a flat
    series (no dispersion) has Sharpe 0 and is treated as normal-shaped."""
    n = len(returns)
    if n < 2:
        raise ValueError(f"need >= 2 returns for a Sharpe ratio; got {n}")
    mean = fmean(returns)
    sd = math.sqrt(fmean([(x - mean) ** 2 for x in returns]))
    if sd == 0.0:
        return 0.0, 0.0, 3.0, n
    m3 = fmean([(x - mean) ** 3 for x in returns])
    m4 = fmean([(x - mean) ** 4 for x in returns])
    return mean / sd, m3 / sd**3, m4 / sd**4, n


def probabilistic_sharpe_ratio(returns: Sequence[float], *, benchmark_sr: float = 0.0) -> float:
    """P(true Sharpe > ``benchmark_sr``) for ``returns`` (Bailey & LdP). The observed
    Sharpe's standard error is widened by negative skew and excess kurtosis, so a
    skewed / fat-tailed series needs a higher observed Sharpe to clear the same bar."""
    sr, skew, kurt, n = _return_stats(returns)
    # SE² term; >= 0 by the moment inequality kurtosis >= skewness² + 1 (equality only
    # for a degenerate two-point series, which the guard handles without a div-by-zero).
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr**2
    if denom <= 0.0:  # pragma: no cover - unreachable except a measure-zero two-point series
        return 0.0
    z = (sr - benchmark_sr) * math.sqrt(n - 1) / math.sqrt(denom)
    return _NORMAL.cdf(z)


def expected_max_sharpe(n_trials: int, sharpe_variance: float) -> float:
    """SR₀ — the Sharpe expected as the best of ``n_trials`` i.i.d. trials by chance (the
    multiple-testing deflation), given the cross-trial Sharpe variance. Zero for a single
    trial or no cross-trial dispersion (no selection has happened)."""
    if n_trials <= 1 or sharpe_variance <= 0.0:
        return 0.0
    z1 = _NORMAL.inv_cdf(1.0 - 1.0 / n_trials)
    z2 = _NORMAL.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(sharpe_variance) * ((1.0 - _EULER_MASCHERONI) * z1 + _EULER_MASCHERONI * z2)


def deflated_sharpe_ratio(
    returns: Sequence[float], *, n_trials: int, trial_sharpe_variance: float
) -> float:
    """DSR — P(true Sharpe > 0) after deflating for selection across ``n_trials`` (Bailey
    & LdP). The benchmark is ``expected_max_sharpe(n_trials, variance)``, so more trials
    in the cell raise the bar and shrink the DSR; trials in *other* cells never enter (the
    count is per-``(market, family, window)``, R4)."""
    sr0 = expected_max_sharpe(n_trials, trial_sharpe_variance)
    return probabilistic_sharpe_ratio(returns, benchmark_sr=sr0)
