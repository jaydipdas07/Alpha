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
    report = run_calibration(
        n_candidates=c.n_candidates,
        n_obs=c.n_obs,
        edge_drift=c.edge_drift,
        oos_fraction=c.oos_fraction,
        dsr_threshold=cfg.dsr.threshold,
        seed=1,
    )
    # the dangerous error — promoting noise/overfit — stays under the strict bound...
    assert report.false_promote_rate <= c.false_promote_max
    # ...and the cheaper error — rejecting a real edge — under the looser bound.
    assert report.false_reject_rate <= c.false_reject_max
    assert report.passes(false_promote_max=c.false_promote_max, false_reject_max=c.false_reject_max)
    # concretely: the gate rejects every noise/overfit control and promotes ~all the edge.
    assert report.noise.promote_rate == 0.0
    assert report.overfit.promote_rate == 0.0
    assert report.edge.promote_rate >= 0.9
    assert report.edge.reject_rate == report.false_reject_rate


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
