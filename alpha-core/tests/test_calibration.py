"""Population-calibration harness (R14, B1a.9) — the rigor gate meets the Tier-2 error rates."""

from __future__ import annotations

import statistics

import pytest
from pydantic import ValidationError

from alpha_core.helpers.config import CalibrationConfig, load_rigor_config
from alpha_core.research.calibration import (
    CalibrationReport,
    calibrate,
    edge_population,
    noise_population,
    overfit_population,
    population_pbo,
    run_calibration,
)

# --- the Done-when: measured rates meet the [You]-ratified Tier-2 thresholds ----


def test_gate_meets_the_tier2_error_rate_thresholds() -> None:
    cfg = load_rigor_config()
    c = cfg.calibration
    # The measured error RATE is the MEAN over a seed sweep, not a single (favorable) draw — the
    # honest Done-when: the gate's true false-promote / false-reject rates meet the [You]-ratified
    # Tier-2 bounds. (The B1a.4 lesson: never certify a statistic off one seed.)
    reports = [
        run_calibration(
            n_candidates=c.n_candidates,
            n_obs=c.n_obs,
            edge_drift=c.edge_drift,
            oos_fraction=c.oos_fraction,
            dsr_threshold=cfg.dsr.threshold,
            seed=s,
        )
        for s in range(20)
    ]
    mean_false_promote = statistics.fmean(r.false_promote_rate for r in reports)
    mean_false_reject = statistics.fmean(r.false_reject_rate for r in reports)
    assert mean_false_promote <= c.false_promote_max  # the dangerous error
    assert mean_false_reject <= c.false_reject_max  # the cheaper error
    # The dangerous error has FULL margin at threshold 0.92: no noise/overfit control is promoted
    # on any seed (the deflation centers them at DSR ~0.5, well below the bar).
    assert mean_false_promote == 0.0
    assert all(r.edge.reject_rate == r.false_reject_rate for r in reports)
    # at this operating point every seed individually clears both bounds, too.
    assert all(
        r.passes(false_promote_max=c.false_promote_max, false_reject_max=c.false_reject_max)
        for r in reports
    )


# --- the holdout gate's operating point: judge the FULL holdout window (rigor.yaml holdout_eval) --

# Holdout-shaped inputs: the current daily crypto holdout is ~224 bars (~7.5 months) — far from the
# ratified calibration's n_obs=2400 (~6.5y); these tests re-run the R14 harness AT that shape.
_HOLDOUT_N_OBS = 224
_SEEDS = range(20)


def _holdout_shaped(oos_fraction: float) -> list[CalibrationReport]:
    cfg = load_rigor_config()
    return [
        run_calibration(
            n_candidates=cfg.calibration.n_candidates,
            n_obs=_HOLDOUT_N_OBS,
            edge_drift=cfg.calibration.edge_drift,
            oos_fraction=oos_fraction,
            dsr_threshold=cfg.dsr.threshold,
            seed=s,
        )
        for s in _SEEDS
    ]


def test_holdout_shaped_full_fraction_keeps_false_promote_bounded() -> None:
    # the DANGEROUS error at the holdout gate's operating point (oos_fraction 1.0, rigor.yaml
    # holdout_eval): judging the whole 224-bar window must not let noise/overfit through. Measured:
    # false-promote is 0 on every seed (the DSR deflation is sample-size-aware — sqrt(n-1) inside
    # PSR — so a shorter window only makes significance HARDER for noise).
    cfg = load_rigor_config()
    reports = _holdout_shaped(load_rigor_config().holdout_eval.oos_fraction)
    assert statistics.fmean(r.false_promote_rate for r in reports) == 0.0
    assert all(r.false_promote_rate <= cfg.calibration.false_promote_max for r in reports)


def test_holdout_shaped_full_fraction_dominates_the_half_slice() -> None:
    # the power fix: the whole holdout is OOS by construction (the candidate is frozen before the
    # read), so judging all of it over the old 50% slice is a sqrt(2) power gain at zero safety
    # cost. Measured over 20 seeds at n_obs=224 / drift 0.15 (~ann. Sharpe 2.9) / 40 trials:
    # false-reject 0.9825 (half) -> 0.8750 (full), false-promote 0.0 at BOTH. NB the residual
    # false-reject is Finding-1 underpower — a ~7.5-month window cannot certify much weaker edges;
    # the pinned roll-forward holdout GROWS with calendar time, which is the real cure.
    half = _holdout_shaped(0.5)
    full = _holdout_shaped(1.0)
    mean_fr_half = statistics.fmean(r.false_reject_rate for r in half)
    mean_fr_full = statistics.fmean(r.false_reject_rate for r in full)
    assert mean_fr_full < mean_fr_half  # strictly more power...
    assert statistics.fmean(r.false_promote_rate for r in full) == 0.0  # ...at zero safety cost
    assert mean_fr_half == pytest.approx(0.9825)
    assert mean_fr_full == pytest.approx(0.8750)


def test_overfit_control_is_genuinely_overfit_not_noise() -> None:
    # the overfit control must really be overfit (high PBO from the IS->OOS reversal), not merely
    # noise — the B1a.4 lesson, that a control's label must match its statistics.
    overfit = overfit_population(n_candidates=12, n_obs=160, seed=3)
    edge = edge_population(n_candidates=12, n_obs=160, seed=4, drift=0.5)
    assert population_pbo(overfit, n_splits=8) > population_pbo(edge, n_splits=8)


# --- the synthetic populations -------------------------------------------------


def test_population_shapes_and_determinism() -> None:
    a = noise_population(n_candidates=5, n_obs=20, seed=1)
    assert len(a) == 5 and all(len(c) == 20 for c in a)
    assert a == noise_population(n_candidates=5, n_obs=20, seed=1)  # seeded -> reproducible
    assert a != noise_population(n_candidates=5, n_obs=20, seed=2)
    # overfit: the first half (in-sample) beats the second (out-of-sample) — the reversal.
    for candidate in overfit_population(n_candidates=4, n_obs=40, seed=1):
        assert statistics.fmean(candidate[:20]) > statistics.fmean(candidate[20:])
    # edge: a genuine positive drift.
    assert all(
        statistics.fmean(c) > 0
        for c in edge_population(n_candidates=4, n_obs=200, seed=1, drift=0.5)
    )


def test_calibrate_rejects_too_few_candidates() -> None:
    with pytest.raises(ValueError, match=">= 2 candidates"):
        calibrate([[0.1, 0.2, 0.3]], oos_fraction=0.5, dsr_threshold=0.95)


# --- config --------------------------------------------------------------------


def test_calibration_config() -> None:
    c = load_rigor_config().calibration
    assert 0.0 < c.false_promote_max < c.false_reject_max < 1.0  # promote bound stricter
    with pytest.raises(ValidationError):
        CalibrationConfig(
            false_promote_max=0.05,
            false_reject_max=0.25,
            n_candidates=1,  # must be > 1
            n_obs=100,
            edge_drift=0.5,
            oos_fraction=0.5,
        )
