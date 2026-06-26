"""The quant-analyst (B1b.2) — the composed rigor gate: promote / reject / revise."""

from __future__ import annotations

import random
import statistics
from collections import Counter

import pytest
from pydantic import ValidationError

from alpha_core.helpers.config import QuantAnalystConfig, load_rigor_config
from alpha_core.research.calibration import (
    _oos,
    edge_population,
    noise_population,
    overfit_population,
)
from alpha_core.research.quant_analyst import Assessment, QuantAnalyst, Verdict, _sharpe


def _deflation(population: list[list[float]], oos_fraction: float = 0.5) -> tuple[int, float]:
    """The cell's DSR deflation inputs — the trial count + the cross-trial Sharpe variance."""
    variance = statistics.pvariance([_sharpe(_oos(c, oos_fraction)) for c in population])
    return len(population), variance


def _verdicts(qa: QuantAnalyst, population: list[list[float]]) -> Counter[Verdict]:
    n, var = _deflation(population)
    return Counter(qa.assess(c, n_trials=n, trial_sharpe_variance=var).verdict for c in population)


# --- the Done-when: rejects a planted overfit; promotes a planted edge -------------------------


def test_promotes_a_planted_edge_and_rejects_a_planted_overfit() -> None:
    qa = QuantAnalyst()
    edge = edge_population(n_candidates=20, n_obs=800, seed=1, drift=0.3)
    overfit = overfit_population(n_candidates=20, n_obs=800, seed=2)
    noise = noise_population(n_candidates=20, n_obs=800, seed=3)
    # a planted edge is promoted; a planted overfit is rejected (its poor OOS fails the pre-screen,
    # exactly as B1a.9 found); pure noise is never promoted.
    assert _verdicts(qa, edge)[Verdict.PROMOTE] == 20
    assert _verdicts(qa, overfit)[Verdict.REJECT] == 20
    assert _verdicts(qa, noise)[Verdict.PROMOTE] == 0


# --- the three verdicts ------------------------------------------------------------------------


def test_a_weak_but_positive_edge_is_revised_not_promoted() -> None:
    # a marginal edge — a positive OOS tilt that isn't significant after deflation -> REVISE
    # (the conservative gate's "real-looking but not yet confirmed"), not promoted.
    counts = _verdicts(
        QuantAnalyst(), edge_population(n_candidates=20, n_obs=400, seed=5, drift=0.08)
    )
    assert counts[Verdict.REVISE] > counts[Verdict.PROMOTE]


def test_a_significant_but_fragile_edge_is_revised() -> None:
    # significant on the OOS slice but inconsistent across CPCV folds (poor in-sample, strong
    # out-of-sample) -> REVISE ("significant but fragile"), not PROMOTE.
    rng = random.Random(0)
    fragile = [rng.gauss(-0.8, 1) for _ in range(240)] + [rng.gauss(0.7, 1) for _ in range(240)]
    cfg = load_rigor_config()
    a = QuantAnalyst().assess(fragile, n_trials=5, trial_sharpe_variance=0.05)
    assert a.verdict is Verdict.REVISE
    assert a.deflated_sharpe >= cfg.dsr.threshold  # it IS significant on the OOS slice...
    assert a.cpcv_robustness < cfg.quant_analyst.cpcv_robustness_min  # ...but fragile across folds


def test_an_overfit_cell_downgrades_a_candidate_to_revise() -> None:
    # even a significant candidate is downgraded to REVISE when the cell's PBO flags the SELECTION
    # as overfit (the optional cell-level check via cell_performance).
    qa = QuantAnalyst()
    overfit = overfit_population(n_candidates=12, n_obs=160, seed=3)
    matrix = [[overfit[k][t] for k in range(len(overfit))] for t in range(len(overfit[0]))]
    candidate = edge_population(n_candidates=1, n_obs=160, seed=9, drift=0.4)[0]
    a = qa.assess(candidate, n_trials=3, trial_sharpe_variance=0.05, cell_performance=matrix)
    assert a.verdict is Verdict.REVISE
    assert a.pbo is not None and a.pbo > load_rigor_config().pbo.threshold


# --- evidence + config -------------------------------------------------------------------------


def test_assessment_carries_the_gate_evidence() -> None:
    qa = QuantAnalyst()
    edge = edge_population(n_candidates=20, n_obs=800, seed=1, drift=0.3)
    n, var = _deflation(edge)
    a = qa.assess(edge[0], n_trials=n, trial_sharpe_variance=var)
    assert isinstance(a, Assessment)
    assert a.verdict is Verdict.PROMOTE
    assert a.oos_sharpe > 0
    assert a.deflated_sharpe >= load_rigor_config().dsr.threshold
    assert a.cpcv_robustness >= load_rigor_config().quant_analyst.cpcv_robustness_min
    assert a.n_trials == 20
    assert a.pbo is None  # no cell matrix was supplied


def test_quant_analyst_config() -> None:
    assert 0.0 <= load_rigor_config().quant_analyst.cpcv_robustness_min <= 1.0
    with pytest.raises(ValidationError):
        QuantAnalystConfig(cpcv_robustness_min=1.5)  # must be in [0, 1]


def test_sharpe_degenerate_cases() -> None:
    assert _sharpe([]) == 0.0  # too few points
    assert _sharpe([3.0, 3.0, 3.0]) == 0.0  # no dispersion
    assert _sharpe([1.0, 2.0, 3.0]) > 0.0
