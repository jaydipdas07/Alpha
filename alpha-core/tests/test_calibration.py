"""Population-calibration harness (R14, B1a.9) — the rigor gate meets the Tier-2 error rates."""

from __future__ import annotations

import statistics

import pytest
from pydantic import ValidationError

from alpha_core.helpers.config import CalibrationConfig, load_rigor_config
from alpha_core.research.calibration import (
    _sharpe,
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


def test_sharpe_degenerate_cases() -> None:
    assert _sharpe([]) == 0.0  # too few points
    assert _sharpe([5.0, 5.0, 5.0]) == 0.0  # no dispersion
    assert _sharpe([1.0, 2.0, 3.0]) > 0.0  # positive mean + real dispersion


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
