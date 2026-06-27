"""The shared ``sharpe`` primitive (#52) — extracted from the triplicated rigor copies."""

from __future__ import annotations

import statistics

from alpha_core.backtest.metrics import sharpe


def test_sharpe_too_few_points_is_zero() -> None:
    # an undefined ratio (0 or 1 points) is no edge, not an error.
    assert sharpe([]) == 0.0
    assert sharpe([0.01]) == 0.0


def test_sharpe_no_dispersion_is_zero() -> None:
    # a constant series has population stdev 0 -> 0.0 (never a div-by-zero).
    assert sharpe([0.02, 0.02, 0.02]) == 0.0


def test_sharpe_is_mean_over_population_stdev() -> None:
    returns = [0.01, -0.005, 0.02, 0.0, 0.015]
    assert sharpe(returns) == statistics.fmean(returns) / statistics.pstdev(returns)
    assert sharpe(returns) > 0  # net positive drift


def test_sharpe_sign_follows_the_mean() -> None:
    assert sharpe([-0.01, -0.02, -0.015, 0.0]) < 0  # net negative drift
