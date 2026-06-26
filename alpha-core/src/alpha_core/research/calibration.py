"""Population-calibration harness (R14, B1a.9) — measure the rigor gate's error rates.

Before the strategist is trusted to generate strategies (the Phase-1b gate), the rigor gate
must be calibrated against **control populations** whose truth we know:

- **noise** — no edge (pure random); the gate must *reject* them. A FALSE PROMOTE is a noise
  candidate the gate let through.
- **overfit** — looks great in-sample but reverses out-of-sample (the midpoint flip); the gate
  must *reject* them too (another false-promote if it doesn't).
- **edge** — a genuine planted signal; the gate must *promote* them. A FALSE REJECT is a real
  edge the gate threw away.

The per-candidate gate is the **Deflated Sharpe Ratio on the out-of-sample slice**, deflated by
the population's trial count (the multiple-testing penalty, R4) — promote iff ``DSR >= threshold``;
an overfit candidate's OOS slice is bad, so the deflated-OOS gate rejects it just as it rejects
noise. ``population_pbo`` (CSCV) is the separate population-level overfitting diagnostic that
*confirms* the overfit control really is overfit (high PBO) versus a genuine edge.

**Scope (honest framing):** this calibrates the per-candidate **DSR-on-OOS** filter — the
statistical-significance gate — not the full composed pipeline (pre-screen → CPCV/PBO → DSR →
holdout). Composing the omitted filters only *rejects more*, so the measured false-promote is an
upper bound and the false-reject is a **lower bound** on the composed gate's. The gate is
conservative **by design** (the R4 deflation refuses to confirm a marginal edge after many
trials), so it is calibrated at a *realistic strong-edge / long-OOS* operating point ([You] chose
to loosen the gate — a lower DSR threshold + a realistic horizon — so a Sharpe-~2.4 edge clears
the false-reject bound while noise/overfit stay rejected). **Sensitivity floor:** this certifies
retention of *strong* edges only — at this operating point an annualized Sharpe >= ~2.4 clears,
but weaker edges (Sharpe ~1-2) are rejected 48-92% of the time; read the result as "the gate
retains strong edges", not "all real edges". An OOS-only gate rejects an overfit
candidate for the same reason it rejects noise (poor OOS), so the overfit control's distinct value
is ``population_pbo`` *confirming* it is genuinely overfit, not the (redundant) reject itself.

Pure-stdlib (seeded ``random``; reuses the B1a.4/B1a.5 rigor). Research-plane only. The Tier-2
error-rate thresholds the measured rates must meet ([You]-ratified) live in ``config/rigor.yaml``.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from alpha_core.backtest.dsr import deflated_sharpe_ratio
from alpha_core.backtest.overfitting import probability_of_backtest_overfitting

Population = list[list[float]]  # a list of candidates, each a per-bucket return series


def noise_population(*, n_candidates: int, n_obs: int, seed: int) -> Population:
    """``n_candidates`` pure-noise return series (no edge) — the gate must reject these."""
    rng = random.Random(seed)
    return [[rng.gauss(0.0, 1.0) for _ in range(n_obs)] for _ in range(n_candidates)]


def overfit_population(
    *, n_candidates: int, n_obs: int, seed: int, amplitude: float = 2.0
) -> Population:
    """``n_candidates`` overfit return series: a per-candidate edge that **reverses sign at the
    midpoint**, so the in-sample (first half) looks great while the out-of-sample (second half)
    is actively bad — the textbook overfit the gate must reject."""
    rng = random.Random(seed)
    half = n_obs // 2
    out: Population = []
    for i in range(n_candidates):
        scale = amplitude * (i + 1) / n_candidates  # spread amplitudes for a real IS ranking
        out.append([(scale if t < half else -scale) + rng.gauss(0.0, 1.0) for t in range(n_obs)])
    return out


def edge_population(*, n_candidates: int, n_obs: int, seed: int, drift: float) -> Population:
    """``n_candidates`` genuine-edge return series (a consistent positive ``drift``) — the gate
    must promote these."""
    rng = random.Random(seed)
    return [[rng.gauss(drift, 1.0) for _ in range(n_obs)] for _ in range(n_candidates)]


def _sharpe(returns: Sequence[float]) -> float:
    if len(returns) < 2:
        return 0.0
    sd = statistics.pstdev(returns)
    return statistics.fmean(returns) / sd if sd > 0 else 0.0


def _oos(returns: Sequence[float], oos_fraction: float) -> list[float]:
    """The out-of-sample tail the gate judges on (a held-out slice, à la B1a.6)."""
    cut = len(returns) - max(2, round(len(returns) * oos_fraction))
    return list(returns[cut:])


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    """One control population run through the gate."""

    n_candidates: int
    promote_rate: float  # fraction the gate PROMOTED

    @property
    def reject_rate(self) -> float:
        return 1.0 - self.promote_rate


def calibrate(
    population: Population, *, oos_fraction: float, dsr_threshold: float
) -> CalibrationResult:
    """Run a control ``population`` through the gate and report the promote rate. The gate
    promotes a candidate iff the Deflated Sharpe Ratio of its out-of-sample slice — with the
    multiple-testing penalty set by the population's trial count and the cross-candidate OOS
    Sharpe variance — clears ``dsr_threshold``."""
    n = len(population)
    if n < 2:
        raise ValueError(f"need >= 2 candidates to calibrate; got {n}")
    oos = [_oos(c, oos_fraction) for c in population]
    variance = statistics.pvariance([_sharpe(o) for o in oos])
    promoted = sum(
        deflated_sharpe_ratio(o, n_trials=n, trial_sharpe_variance=variance) >= dsr_threshold
        for o in oos
    )
    return CalibrationResult(n_candidates=n, promote_rate=promoted / n)


def population_pbo(population: Population, *, n_splits: int) -> float:
    """The CSCV Probability of Backtest Overfitting over the population's full-series matrix —
    the overfitting diagnostic (high when selecting among these candidates is overfit)."""
    width = min(len(c) for c in population)
    matrix = [[candidate[t] for candidate in population] for t in range(width)]
    return probability_of_backtest_overfitting(matrix, n_splits=n_splits).pbo


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """The full calibration verdict against the [You]-ratified Tier-2 error-rate thresholds."""

    noise: CalibrationResult
    overfit: CalibrationResult
    edge: CalibrationResult
    false_promote_rate: float  # worst (max) promote rate across the noise + overfit controls
    false_reject_rate: float  # the edge control's reject rate

    def passes(self, *, false_promote_max: float, false_reject_max: float) -> bool:
        """True iff the gate is calibrated: it rarely promotes noise/overfit AND rarely rejects
        a real edge (the Phase-1b gate, Tier-2)."""
        return (
            self.false_promote_rate <= false_promote_max
            and self.false_reject_rate <= false_reject_max
        )


def run_calibration(
    *,
    n_candidates: int,
    n_obs: int,
    edge_drift: float,
    oos_fraction: float,
    dsr_threshold: float,
    seed: int,
) -> CalibrationReport:
    """Generate the three control populations, run each through the gate, and summarize the
    false-promote (noise + overfit) and false-reject (edge) rates."""
    noise = calibrate(
        noise_population(n_candidates=n_candidates, n_obs=n_obs, seed=seed),
        oos_fraction=oos_fraction,
        dsr_threshold=dsr_threshold,
    )
    overfit = calibrate(
        overfit_population(n_candidates=n_candidates, n_obs=n_obs, seed=seed + 1),
        oos_fraction=oos_fraction,
        dsr_threshold=dsr_threshold,
    )
    edge = calibrate(
        edge_population(n_candidates=n_candidates, n_obs=n_obs, seed=seed + 2, drift=edge_drift),
        oos_fraction=oos_fraction,
        dsr_threshold=dsr_threshold,
    )
    return CalibrationReport(
        noise=noise,
        overfit=overfit,
        edge=edge,
        false_promote_rate=max(noise.promote_rate, overfit.promote_rate),
        false_reject_rate=edge.reject_rate,
    )
