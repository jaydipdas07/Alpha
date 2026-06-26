"""Deflated / Probabilistic Sharpe Ratio (B1a.5) — Bailey & López de Prado (2014)."""

from __future__ import annotations

import math
import random
from statistics import NormalDist

import pytest
from pydantic import ValidationError

from alpha_core.backtest.dsr import (
    _return_stats,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    probabilistic_sharpe_ratio,
)
from alpha_core.helpers.config import DSRConfig, load_rigor_config


def _normalish(n: int, seed: int, drift: float) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(drift, 1.0) for _ in range(n)]


# --- return moments ------------------------------------------------------------


def test_return_stats_symmetric() -> None:
    sr, skew, _, n = _return_stats([1.0, -1.0, 1.0, -1.0])
    assert n == 4
    assert sr == 0.0  # zero mean
    assert abs(skew) < 1e-9  # symmetric -> no skew


def test_return_stats_flat_series_is_zero_sharpe() -> None:
    sr, skew, kurt, n = _return_stats([0.5, 0.5, 0.5])  # no dispersion
    assert sr == 0.0 and skew == 0.0 and kurt == 3.0 and n == 3


def test_return_stats_rejects_too_few() -> None:
    with pytest.raises(ValueError, match=">= 2 returns"):
        _return_stats([1.0])


# --- probabilistic Sharpe ------------------------------------------------------


def test_psr_high_for_strong_long_series() -> None:
    psr = probabilistic_sharpe_ratio(_normalish(500, seed=1, drift=0.25))
    assert 0.0 <= psr <= 1.0
    assert psr > 0.95  # a clear positive edge over many obs is significant


def test_psr_decreases_with_a_higher_benchmark() -> None:
    returns = _normalish(500, seed=2, drift=0.15)
    assert probabilistic_sharpe_ratio(returns, benchmark_sr=0.0) > probabilistic_sharpe_ratio(
        returns, benchmark_sr=0.2
    )


def test_psr_penalizes_negative_skew_and_fat_tails() -> None:
    returns = [0.3] * 95 + [-2.0] * 5  # positive mean, a severe left tail
    sr, skew, kurt, _ = _return_stats(returns)
    assert sr > 0 and skew < 0 and kurt > 3
    psr = probabilistic_sharpe_ratio(returns, benchmark_sr=0.0)
    naive = NormalDist().cdf(sr * math.sqrt(len(returns) - 1))  # ignores higher moments
    assert psr < naive  # the skew / kurtosis correction lowers significance


# --- expected maximum Sharpe (the deflation) -----------------------------------


def test_expected_max_sharpe_grows_with_trials() -> None:
    var = 0.25
    assert expected_max_sharpe(1, var) == 0.0  # a single trial: no selection
    assert expected_max_sharpe(2, 0.0) == 0.0  # no cross-trial dispersion
    s2, s10, s100 = (expected_max_sharpe(n, var) for n in (2, 10, 100))
    assert 0.0 < s2 < s10 < s100  # more trials -> higher expected max -> bigger penalty


# --- deflated Sharpe -----------------------------------------------------------


def test_dsr_deflates_with_more_trials() -> None:
    returns = _normalish(500, seed=3, drift=0.18)
    var = 0.25
    few = deflated_sharpe_ratio(returns, n_trials=2, trial_sharpe_variance=var)
    many = deflated_sharpe_ratio(returns, n_trials=5000, trial_sharpe_variance=var)
    assert 0.0 <= many <= few <= 1.0
    assert many < few  # the same edge is less convincing after many trials
    # n_trials=1 means no deflation -> DSR == PSR measured against 0.
    assert deflated_sharpe_ratio(
        returns, n_trials=1, trial_sharpe_variance=var
    ) == probabilistic_sharpe_ratio(returns, benchmark_sr=0.0)


# --- config --------------------------------------------------------------------


def test_dsr_config() -> None:
    cfg = load_rigor_config()
    assert 0.0 < cfg.dsr.threshold < 1.0
    with pytest.raises(ValidationError):
        DSRConfig(threshold=1.5)  # outside (0, 1)
