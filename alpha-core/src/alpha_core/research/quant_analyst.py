"""The quant-analyst (B1b.2) — adjudicate a candidate through the composed rigor gate.

The strategist (B1b.1) proposes; the quant-analyst judges. It runs a candidate's backtest return
series through the Phase-1a rigor primitives and returns a promote / reject / revise verdict with
the evidence. **The Deflated Sharpe Ratio does the promote-work** (the B1a.9-calibrated significance
gate); every other check only ever *withholds* a promote, never grants one — so the calibrated
<5% false-promote bound is preserved by construction.

* **Coarse OOS-edge screen** — the candidate's out-of-sample Sharpe. At or below ``min_oos_sharpe``
  there is no out-of-sample edge → **REJECT**. This is the only path to REJECT, and it is also how a
  high-in-sample / poor-out-of-sample *overfit* dies — via its poor OOS, **not** via overfit
  detection (honest about what does the work). This is the analyst's own screen, *not* the B1a.8
  windowed vectorbt pre-screen.
* **Deflated Sharpe Ratio (B1a.5, R4)** — the OOS Sharpe deflated by the cell's *cumulative trial
  count*, vs ``dsr.threshold`` (0.92, ratified at 1a.GATE). Below it the edge is not significant
  after accounting for how many configs were tried in the (market, family, window) cell → REVISE.
* **Fold consistency** — the fraction of the combinatorial test folds (``cpcv_splits``) whose Sharpe
  is positive: a regime-consistency signal (a real edge holds across folds). It re-fits nothing and
  uses no purge — it is **not** purged cross-validation, just fold positivity; its threshold
  ``fold_consistency_min`` is a judgment call, not B1a.9-calibrated. Significant-but-inconsistent →
  REVISE.
* **PBO (B1a.4, optional)** — given the cell's (time x trial) matrix, the Probability of Backtest
  Overfitting flags an overfit *selection* → REVISE. A confirmatory cell-level signal, not a
  primary gate.

Verdict: **REJECT** (no out-of-sample edge) · **PROMOTE** (significant + consistent + the cell is
not overfit) · **REVISE** (real-looking but unconfirmed — not significant after deflation,
fold-fragile, or from an overfit cell). A positive-but-unconfirmed candidate is REVISE, never
REJECT: the analyst can't tell lucky noise from a weak real edge, so it asks for more evidence
rather than discarding.

The DSR deflation inputs (``n_trials`` + ``trial_sharpe_variance``) carry the whole multiple-testing
safety — pass ``n_trials=1`` and the penalty vanishes. Always derive them with ``deflation_inputs``
(the calibration-faithful computation), never by hand.

Research-plane only, pure-stdlib (float return series — money stays ``Decimal`` on the order/P&L
path). It judges the in-sample/out-of-sample returns the backtest produced; the one-shot locked
holdout (B1a.6) stays sealed for the final pre-live gate.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from alpha_core.backtest.dsr import deflated_sharpe_ratio
from alpha_core.backtest.metrics import sharpe
from alpha_core.backtest.overfitting import (
    cpcv_splits,
    probability_of_backtest_overfitting,
)
from alpha_core.helpers.config import RigorConfig, load_rigor_config


class Verdict(StrEnum):
    """The quant-analyst's call on a candidate."""

    PROMOTE = "promote"
    REVISE = "revise"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class Assessment:
    """A verdict plus the evidence behind it (every gate's measured value)."""

    verdict: Verdict
    reason: str
    oos_sharpe: float  # coarse out-of-sample Sharpe (the edge screen)
    deflated_sharpe: float  # DSR vs the cell's trial count (0.0 if rejected at the screen)
    fold_consistency: float  # fraction of combinatorial test folds with positive Sharpe
    n_trials: int  # the cell's cumulative trial count the DSR deflated by
    pbo: float | None = None  # cell-level PBO, when the cell trial matrix was supplied


def _oos(returns: Sequence[float], oos_fraction: float) -> list[float]:
    """The trailing ``oos_fraction`` of the series (>= 2 points) — the same slice the B1a.9
    calibration judges on."""
    return list(returns[-max(2, round(len(returns) * oos_fraction)) :])


def deflation_inputs(
    population: Sequence[Sequence[float]], *, oos_fraction: float
) -> tuple[int, float]:
    """The DSR deflation inputs for a cell's trial population — the trial count and the cross-trial
    OOS Sharpe variance — computed *exactly* as the B1a.9 calibration does, so the discovery-loop
    caller and the calibration can never diverge. Feed the result to ``assess``."""
    sharpes = [sharpe(_oos(trial, oos_fraction)) for trial in population]
    variance = statistics.pvariance(sharpes) if sharpes else 0.0
    return len(population), variance


class QuantAnalyst:
    """Adjudicate a candidate's returns through the composed rigor gate (see module docstring)."""

    def __init__(self, config: RigorConfig | None = None) -> None:
        cfg = config if config is not None else load_rigor_config()
        self._dsr = cfg.dsr
        self._cpcv = cfg.cpcv
        self._pbo = cfg.pbo
        self._qa = cfg.quant_analyst

    @property
    def oos_fraction(self) -> float:
        """The out-of-sample slice this analyst judges on — callers (the discovery loop) compute
        the cell's ``deflation_inputs`` on the *same* slice, so the deflation can't desync."""
        return self._qa.oos_fraction

    def assess(
        self,
        returns: Sequence[float],
        *,
        n_trials: int,
        trial_sharpe_variance: float,
        oos_fraction: float | None = None,
        cell_performance: Sequence[Sequence[float]] | None = None,
    ) -> Assessment:
        """Adjudicate ``returns`` (a candidate's backtest return series) for a cell with
        ``n_trials`` cumulative trials and cross-trial Sharpe variance ``trial_sharpe_variance``
        (derive both via ``deflation_inputs``). Optionally pass the cell's ``(time x trial)``
        matrix for the PBO check. Raises ``ValueError`` on inputs that would disable a gate."""
        of = self._qa.oos_fraction if oos_fraction is None else oos_fraction
        min_obs = 2 * self._cpcv.n_groups
        if n_trials < 1:
            raise ValueError(f"n_trials must be >= 1 (the cell's trial count); got {n_trials}")
        if trial_sharpe_variance < 0:
            raise ValueError(f"trial_sharpe_variance must be >= 0; got {trial_sharpe_variance}")
        # 1.0 is legitimate: the holdout gate judges its WHOLE window (all of it is OOS to a
        # frozen candidate, rigor.yaml holdout_eval); an in-sample assessment passes < 1.
        if not 0.0 < of <= 1.0:
            raise ValueError(f"oos_fraction must be in (0, 1]; got {of}")
        if len(returns) < min_obs:
            raise ValueError(f"need >= {min_obs} observations to assess; got {len(returns)}")

        oos = _oos(returns, of)
        oos_sharpe = sharpe(oos)

        # 1. coarse OOS-edge screen — no OOS edge (incl. a high-IS/poor-OOS overfit): reject.
        if oos_sharpe <= self._qa.min_oos_sharpe:
            return Assessment(
                Verdict.REJECT,
                f"no out-of-sample edge (OOS Sharpe {oos_sharpe:.3f} <= {self._qa.min_oos_sharpe})",
                oos_sharpe,
                0.0,
                0.0,
                n_trials,
            )

        # 2. DSR significance vs the cell's trial count; 3. fold consistency across the folds.
        dsr = deflated_sharpe_ratio(
            oos, n_trials=n_trials, trial_sharpe_variance=trial_sharpe_variance
        )
        consistency = self._fold_consistency(returns)

        # 4. optional cell-level PBO (selection overfitting).
        pbo: float | None = None
        if cell_performance is not None:
            pbo = probability_of_backtest_overfitting(
                cell_performance, n_splits=self._pbo.n_splits
            ).pbo

        if pbo is not None and pbo > self._pbo.threshold:
            return Assessment(
                Verdict.REVISE,
                f"cell selection looks overfit (PBO {pbo:.2f} > {self._pbo.threshold})",
                oos_sharpe,
                dsr,
                consistency,
                n_trials,
                pbo,
            )
        if dsr < self._dsr.threshold:
            return Assessment(
                Verdict.REVISE,
                f"edge not significant after deflating for {n_trials} trials "
                f"(DSR {dsr:.2f} < {self._dsr.threshold})",
                oos_sharpe,
                dsr,
                consistency,
                n_trials,
                pbo,
            )
        if consistency < self._qa.fold_consistency_min:
            return Assessment(
                Verdict.REVISE,
                f"significant but fragile across folds ({consistency:.0%} positive < "
                f"{self._qa.fold_consistency_min:.0%})",
                oos_sharpe,
                dsr,
                consistency,
                n_trials,
                pbo,
            )
        return Assessment(
            Verdict.PROMOTE,
            "significant, consistent across folds, and the cell is not overfit",
            oos_sharpe,
            dsr,
            consistency,
            n_trials,
            pbo,
        )

    def _fold_consistency(self, returns: Sequence[float]) -> float:
        """Fraction of the combinatorial test folds whose Sharpe is positive — a regime-consistency
        signal (a real edge holds across folds, a fragile one does not). Uses the ``cpcv_splits``
        fold partition for the test slices only; it re-fits nothing and applies no purge, so it is
        *not* purged cross-validation."""
        splits = cpcv_splits(
            len(returns),
            n_groups=self._cpcv.n_groups,
            n_test_groups=self._cpcv.n_test_groups,
            embargo_frac=self._cpcv.embargo_frac,
        )  # always >= 1 split once len(returns) >= 2 * n_groups (validated in assess)
        positive = sum(1 for sp in splits if sharpe([returns[i] for i in sp.test]) > 0)
        return positive / len(splits)
