"""The quant-analyst (B1b.2) — the composed rigor gate: promote / reject / revise."""

from __future__ import annotations

import random
import statistics

import pytest
from pydantic import ValidationError

from alpha_core.backtest.metrics import sharpe
from alpha_core.helpers.config import QuantAnalystConfig, load_rigor_config
from alpha_core.research.calibration import _oos as _cal_oos
from alpha_core.research.calibration import (
    edge_population,
    noise_population,
    overfit_population,
)
from alpha_core.research.quant_analyst import (
    Assessment,
    QuantAnalyst,
    Verdict,
    deflation_inputs,
)


def _rate(qa: QuantAnalyst, population: list[list[float]], verdict: Verdict) -> int:
    n, var = deflation_inputs(population, oos_fraction=0.5)
    return sum(
        qa.assess(c, n_trials=n, trial_sharpe_variance=var).verdict == verdict for c in population
    )


# --- the Done-when: rejects a planted overfit; promotes a planted edge -------------------------


def test_promotes_a_planted_edge_and_rejects_a_planted_overfit() -> None:
    # Tested at the B1a.9 calibration operating point (drift 0.15 ~ Sharpe 2.4, n_obs 2400) and as
    # a RATE over several seeds — never a single (favorable) draw (the B1a.4 lesson).
    qa = QuantAnalyst()
    promoted = overfit_rejected = noise_promoted = 0
    seeds = range(3)
    for s in seeds:
        promoted += _rate(
            qa, edge_population(n_candidates=10, n_obs=2400, seed=s, drift=0.15), Verdict.PROMOTE
        )
        overfit_rejected += _rate(
            qa, overfit_population(n_candidates=10, n_obs=2400, seed=s + 100), Verdict.REJECT
        )
        noise_promoted += _rate(
            qa, noise_population(n_candidates=10, n_obs=2400, seed=s + 200), Verdict.PROMOTE
        )
    total = 10 * len(seeds)
    assert promoted / total >= 0.85  # a realistic planted edge is promoted (the conservative gate)
    assert (
        overfit_rejected == total
    )  # every planted overfit rejected (its poor OOS fails the screen)
    assert noise_promoted == 0  # pure noise is never promoted


# --- the three verdicts ------------------------------------------------------------------------


def test_a_weak_but_positive_edge_is_revised_not_promoted() -> None:
    # a marginal edge — a positive OOS tilt that isn't significant after deflation -> REVISE
    # (the conservative gate's "real-looking but not yet confirmed"), not promoted.
    qa = QuantAnalyst()
    edge = edge_population(n_candidates=20, n_obs=400, seed=5, drift=0.08)
    assert _rate(qa, edge, Verdict.REVISE) > _rate(qa, edge, Verdict.PROMOTE)


def test_a_significant_but_fragile_edge_is_revised() -> None:
    # significant on the OOS slice but inconsistent across folds (poor in-sample, strong
    # out-of-sample) -> REVISE ("significant but fragile"), not PROMOTE.
    rng = random.Random(0)
    fragile = [rng.gauss(-0.8, 1) for _ in range(240)] + [rng.gauss(0.7, 1) for _ in range(240)]
    cfg = load_rigor_config()
    a = QuantAnalyst().assess(fragile, n_trials=5, trial_sharpe_variance=0.05)
    assert a.verdict is Verdict.REVISE
    assert a.deflated_sharpe >= cfg.dsr.threshold  # it IS significant on the OOS slice...
    assert (
        a.fold_consistency < cfg.quant_analyst.fold_consistency_min
    )  # ...but fragile across folds


def test_an_overfit_cell_downgrades_a_candidate_to_revise() -> None:
    # even a significant candidate is downgraded to REVISE when the cell's PBO flags the SELECTION
    # as overfit (the optional cell-level check). The overfit cell has robustly high PBO (>0.7).
    qa = QuantAnalyst()
    overfit = overfit_population(n_candidates=20, n_obs=400, seed=2)
    matrix = [[overfit[k][t] for k in range(len(overfit))] for t in range(len(overfit[0]))]
    candidate = edge_population(n_candidates=1, n_obs=400, seed=9, drift=0.4)[0]
    a = qa.assess(candidate, n_trials=3, trial_sharpe_variance=0.05, cell_performance=matrix)
    assert a.verdict is Verdict.REVISE
    assert a.pbo is not None and a.pbo > load_rigor_config().pbo.threshold


# --- inputs, evidence, config ------------------------------------------------------------------


def test_assess_validates_its_deflation_inputs() -> None:
    # the deflation inputs carry the whole multiple-testing safety: reject inputs that would
    # silently disable a gate, rather than quietly over-promoting.
    qa = QuantAnalyst()
    good = [0.1 * i for i in range(50)]
    with pytest.raises(ValueError, match="n_trials must be"):
        qa.assess(good, n_trials=0, trial_sharpe_variance=0.05)
    with pytest.raises(ValueError, match="trial_sharpe_variance must be"):
        qa.assess(good, n_trials=5, trial_sharpe_variance=-1.0)
    with pytest.raises(ValueError, match="oos_fraction must be"):
        qa.assess(good, n_trials=5, trial_sharpe_variance=0.05, oos_fraction=1.5)
    with pytest.raises(ValueError, match="observations to assess"):
        qa.assess(
            [0.1, 0.2, 0.3], n_trials=5, trial_sharpe_variance=0.05
        )  # too short for the folds


def test_deflation_inputs_matches_the_calibration() -> None:
    pop = edge_population(n_candidates=10, n_obs=400, seed=1, drift=0.2)
    n, var = deflation_inputs(pop, oos_fraction=0.5)
    expected = statistics.pvariance([sharpe(_cal_oos(c, 0.5)) for c in pop])
    assert n == 10
    assert var == pytest.approx(expected)


def test_assessment_carries_the_gate_evidence() -> None:
    qa = QuantAnalyst()
    edge = edge_population(n_candidates=10, n_obs=2400, seed=1, drift=0.15)
    n, var = deflation_inputs(edge, oos_fraction=0.5)
    a = qa.assess(edge[0], n_trials=n, trial_sharpe_variance=var)
    assert isinstance(a, Assessment)
    assert a.verdict is Verdict.PROMOTE
    assert a.oos_sharpe > 0
    assert a.deflated_sharpe >= load_rigor_config().dsr.threshold
    assert a.fold_consistency >= load_rigor_config().quant_analyst.fold_consistency_min
    assert a.n_trials == 10
    assert a.pbo is None  # no cell matrix was supplied


def test_quant_analyst_config() -> None:
    qa = load_rigor_config().quant_analyst
    assert 0.0 <= qa.fold_consistency_min <= 1.0
    with pytest.raises(ValidationError):
        QuantAnalystConfig(min_oos_sharpe=0.0, fold_consistency_min=1.5, oos_fraction=0.5)
