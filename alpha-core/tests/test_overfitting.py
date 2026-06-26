"""Advanced overfitting rigor (B1a.4) — CPCV + embargo + PBO.

The PBO controls are deterministic (seeded ``random.Random``): selecting the best of
many pure-noise trials is the textbook overfit (high PBO); a single genuine edge
generalizes (PBO ~ 0). The config threshold separates them.
"""

from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from alpha_core.backtest.overfitting import (
    _contiguous_groups,
    _sharpe,
    cpcv_splits,
    probability_of_backtest_overfitting,
)
from alpha_core.helpers.config import CPCVConfig, PBOConfig, load_rigor_config

# --- CPCV + embargo ------------------------------------------------------------


def test_cpcv_no_embargo_train_is_exact_complement() -> None:
    splits = cpcv_splits(12, n_groups=6, n_test_groups=2, embargo_frac=0.0)
    assert len(splits) == 15  # C(6,2)
    for s in splits:
        assert len(s.test) == 4  # 2 groups x 2 obs
        assert set(s.train) | set(s.test) == set(range(12))  # exact complement
        assert set(s.train).isdisjoint(s.test)


def test_cpcv_embargo_purges_around_each_test_block() -> None:
    # 12 obs / 6 groups of 2, k=1, embargo_frac=0.1 -> horizon = ceil(1.2) = 2.
    splits = cpcv_splits(12, n_groups=6, n_test_groups=1, embargo_frac=0.1)
    assert len(splits) == 6  # C(6,1)
    by_test = {s.test: s for s in splits}
    # interior group (obs 4,5): purge (2,3) before + embargo (6,7) after.
    assert set(by_test[(4, 5)].train) == {0, 1, 8, 9, 10, 11}
    # the first block has nothing before it -> only the forward embargo (2,3) applies.
    assert set(by_test[(0, 1)].train) == {4, 5, 6, 7, 8, 9, 10, 11}
    for s in splits:
        assert set(s.train).isdisjoint(s.test)


def test_cpcv_rejects_bad_params() -> None:
    with pytest.raises(ValueError, match="n_test_groups < n_groups"):
        cpcv_splits(12, n_groups=6, n_test_groups=6, embargo_frac=0.0)  # k == N
    with pytest.raises(ValueError, match="n_groups <= n_obs"):
        cpcv_splits(3, n_groups=6, n_test_groups=2, embargo_frac=0.0)  # N > n_obs
    with pytest.raises(ValueError, match="embargo_frac"):
        cpcv_splits(12, n_groups=6, n_test_groups=2, embargo_frac=1.5)


# --- PBO via CSCV --------------------------------------------------------------


def _noise_population(t: int, n: int, seed: int) -> list[list[float]]:
    """N independent pure-noise trials (no edge) over T time-buckets."""
    rng = random.Random(seed)
    return [[rng.gauss(0.0, 1.0) for _ in range(n)] for _ in range(t)]


def _edge_population(t: int, n: int, seed: int, drift: float) -> list[list[float]]:
    """Trial 0 carries a genuine positive drift; the rest are pure noise."""
    rng = random.Random(seed)
    return [[rng.gauss(drift if i == 0 else 0.0, 1.0) for i in range(n)] for _ in range(t)]


def _reversing_population(t: int, n: int, seed: int, signal: float = 2.0) -> list[list[float]]:
    """A genuinely overfit population: each trial's edge reverses sign at the midpoint
    (the amplitude is spread across trials), so the in-sample-best trial is the
    out-of-sample-worst. Unlike pure noise — which is *no-skill* (PBO ~ 0.5) — this is
    robustly high-PBO for any seed, the textbook overfit a rigor gate must catch."""
    rng = random.Random(seed)
    half = t // 2
    return [
        [
            (1.0 if ti < half else -1.0) * signal * (i - (n - 1) / 2) + rng.gauss(0.0, 1.0)
            for i in range(n)
        ]
        for ti in range(t)
    ]


def test_pbo_flags_a_known_overfit_control() -> None:
    cfg = load_rigor_config()
    clean = probability_of_backtest_overfitting(
        _edge_population(120, 10, seed=7, drift=0.8), n_splits=cfg.pbo.n_splits
    )
    # A single genuine edge generalizes -> in-sample-best stays OOS-best -> PBO ~ 0.
    assert clean.is_overfit(cfg.pbo.threshold) is False
    assert clean.pbo < 0.2
    # An overfit population (edge reverses out-of-sample) is flagged. Checked across
    # several seeds so the pass is the *method*, never a cherry-picked draw.
    for seed in (1, 2, 3):
        overfit = probability_of_backtest_overfitting(
            _reversing_population(120, 10, seed=seed), n_splits=cfg.pbo.n_splits
        )
        assert overfit.is_overfit(cfg.pbo.threshold) is True
        assert overfit.pbo > 0.7
        assert overfit.pbo > clean.pbo
        assert overfit.n_paths == 252  # C(10,5)
        assert overfit.n_trials == 10


def test_pbo_is_calibrated_no_skill_for_pure_noise() -> None:
    """Independent pure-noise trials are *no-skill*, not overfit: the in-sample-best
    has a uniform out-of-sample rank, so PBO centres on ~0.5 (a single noise draw is
    high-variance, hence the mean over seeds). This is the honest baseline the overfit
    and edge controls are flagged against."""
    cfg = load_rigor_config()
    pbos = [
        probability_of_backtest_overfitting(
            _noise_population(120, 10, seed=s), n_splits=cfg.pbo.n_splits
        ).pbo
        for s in range(20)
    ]
    assert 0.35 <= sum(pbos) / len(pbos) <= 0.65  # ~0.5: selection no better than chance


def test_pbo_is_deterministic() -> None:
    matrix = _noise_population(120, 8, seed=1)
    a = probability_of_backtest_overfitting(matrix, n_splits=10)
    b = probability_of_backtest_overfitting(matrix, n_splits=10)
    assert a.pbo == b.pbo
    assert a.logits == b.logits


def test_pbo_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="empty"):
        probability_of_backtest_overfitting([], n_splits=10)
    with pytest.raises(ValueError, match=">= 2 trials"):
        probability_of_backtest_overfitting([[1.0], [2.0]], n_splits=2)
    with pytest.raises(ValueError, match="rectangular"):
        probability_of_backtest_overfitting([[1.0, 2.0], [3.0]], n_splits=2)
    with pytest.raises(ValueError, match="even"):
        probability_of_backtest_overfitting(_noise_population(20, 4, seed=0), n_splits=5)
    with pytest.raises(ValueError, match="cannot exceed"):
        probability_of_backtest_overfitting(_noise_population(4, 4, seed=0), n_splits=10)


# --- rigor.yaml config ---------------------------------------------------------


def test_load_rigor_config() -> None:
    cfg = load_rigor_config()
    assert cfg.cpcv.n_test_groups < cfg.cpcv.n_groups <= 1_000
    assert 0.0 <= cfg.cpcv.embargo_frac <= 1.0
    assert cfg.pbo.n_splits % 2 == 0
    assert 0.0 <= cfg.pbo.threshold <= 1.0


def test_rigor_config_rejects_invalid() -> None:
    with pytest.raises(ValidationError, match="n_test_groups"):
        CPCVConfig(n_groups=4, n_test_groups=4, embargo_frac=0.0)  # k >= N
    with pytest.raises(ValidationError, match="even"):
        PBOConfig(n_splits=7, threshold=0.5)  # odd


# --- helper contracts ----------------------------------------------------------


def test_sharpe_degenerate_cases() -> None:
    assert _sharpe([]) == 0.0  # no points
    assert _sharpe([1.0]) == 0.0  # too few to define dispersion
    assert _sharpe([3.0, 3.0, 3.0]) == 0.0  # no dispersion (a flat trial)
    assert _sharpe([1.0, 2.0, 3.0]) > 0.0  # positive mean, real dispersion


def test_contiguous_groups_rejects_bad_k() -> None:
    with pytest.raises(ValueError, match="0 < k <= n"):
        _contiguous_groups(5, 0)
    with pytest.raises(ValueError, match="0 < k <= n"):
        _contiguous_groups(3, 5)
