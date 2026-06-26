"""The quant-analyst (B1b.2) — adjudicate a candidate through the *composed* rigor gate.

The strategist (B1b.1) proposes; the quant-analyst judges. It runs a candidate's backtest return
series through the now-trusted rigor primitives (built + calibrated in Phase 1a) and returns a
**promote / reject / revise** verdict with the evidence behind it:

* **Pre-screen (B1a.8)** — a cheap coarse out-of-sample Sharpe. No OOS edge → **REJECT** outright
  (this is also how a *fitted-noise overfit*, high in-sample / poor out-of-sample, is caught).
* **Deflated Sharpe Ratio (B1a.5, R4)** — the candidate's OOS Sharpe deflated by the cell's
  *cumulative trial count* — the calibrated significance gate (``dsr.threshold`` = 0.92, ratified
  at 1a.GATE). Below it the edge is not significant after accounting for how many configs were
  tried in the ``(market, family, window)`` cell.
* **CPCV robustness (B1a.4)** — the fraction of combinatorial-purged folds whose OOS Sharpe is
  positive. A significant edge that holds on too few folds is *fragile*, not promotable.
* **PBO (B1a.4, optional)** — given the cell's trial matrix, the Probability of Backtest
  Overfitting confirms whether the *selection* is overfit (a cell-level signal), downgrading an
  otherwise-significant candidate to a re-test.

The verdict is **REJECT** (no out-of-sample edge), **PROMOTE** (significant + robust + the cell is
not overfit), or **REVISE** (a real-looking edge not yet confirmed — not significant after
deflation, fragile across folds, or drawn from an overfit cell — worth re-parameterizing or more
data). This *composes* the primitives the B1a.9 calibration validated; it re-derives none of them.

Research-plane only, pure-stdlib. It judges the in-sample/out-of-sample returns the backtest
produced — the one-shot locked holdout (B1a.6) stays sealed, used only as the final pre-live gate.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from alpha_core.backtest.dsr import deflated_sharpe_ratio
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
    oos_sharpe: float  # coarse out-of-sample Sharpe (the pre-screen)
    deflated_sharpe: float  # DSR vs the cell's trial count (0.0 if rejected at pre-screen)
    cpcv_robustness: float  # fraction of CPCV test folds with positive Sharpe
    n_trials: int  # the cell's cumulative trial count the DSR deflated by
    pbo: float | None = None  # cell-level PBO, when the cell trial matrix was supplied


def _sharpe(returns: Sequence[float]) -> float:
    """Per-series Sharpe (mean / population stdev); 0 with too few points or no dispersion."""
    if len(returns) < 2:
        return 0.0
    sd = statistics.pstdev(returns)
    return statistics.fmean(returns) / sd if sd > 0 else 0.0


class QuantAnalyst:
    """Adjudicate a candidate's returns through the composed rigor gate (see module docstring)."""

    def __init__(self, config: RigorConfig | None = None) -> None:
        cfg = config if config is not None else load_rigor_config()
        self._prescreen = cfg.prescreen
        self._dsr = cfg.dsr
        self._cpcv = cfg.cpcv
        self._pbo = cfg.pbo
        self._qa = cfg.quant_analyst

    def assess(
        self,
        returns: Sequence[float],
        *,
        n_trials: int,
        trial_sharpe_variance: float,
        oos_fraction: float = 0.5,
        cell_performance: Sequence[Sequence[float]] | None = None,
    ) -> Assessment:
        """Adjudicate ``returns`` (the candidate's backtest return series) for a cell with
        ``n_trials`` cumulative trials and cross-trial Sharpe variance ``trial_sharpe_variance``
        (the DSR deflation inputs, from the ledger + the cell). Optionally pass the cell's
        ``(time x trial)`` performance matrix for the cell-level PBO check."""
        n = len(returns)
        oos = list(returns[-max(2, round(n * oos_fraction)) :])
        oos_sharpe = _sharpe(oos)

        # 1. pre-screen — no out-of-sample edge (incl. high-IS/poor-OOS overfit): reject cheaply.
        if oos_sharpe <= self._prescreen.min_sharpe:
            return Assessment(
                Verdict.REJECT,
                f"no out-of-sample edge (coarse OOS Sharpe {oos_sharpe:.3f})",
                oos_sharpe,
                0.0,
                0.0,
                n_trials,
            )

        # 2. DSR significance vs the cell's trial count; 3. CPCV robustness across purged folds.
        dsr = deflated_sharpe_ratio(
            oos, n_trials=n_trials, trial_sharpe_variance=trial_sharpe_variance
        )
        robustness = self._cpcv_robustness(returns)

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
                robustness,
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
                robustness,
                n_trials,
                pbo,
            )
        if robustness < self._qa.cpcv_robustness_min:
            return Assessment(
                Verdict.REVISE,
                f"significant but fragile across CPCV folds ({robustness:.0%} positive < "
                f"{self._qa.cpcv_robustness_min:.0%})",
                oos_sharpe,
                dsr,
                robustness,
                n_trials,
                pbo,
            )
        return Assessment(
            Verdict.PROMOTE,
            "significant, robust across CPCV folds, and the cell is not overfit",
            oos_sharpe,
            dsr,
            robustness,
            n_trials,
            pbo,
        )

    def _cpcv_robustness(self, returns: Sequence[float]) -> float:
        """Fraction of combinatorial-purged test folds whose Sharpe is positive — a consistency
        (regime-stability) signal: a real edge holds across folds, a fragile one does not."""
        splits = cpcv_splits(
            len(returns),
            n_groups=self._cpcv.n_groups,
            n_test_groups=self._cpcv.n_test_groups,
            embargo_frac=self._cpcv.embargo_frac,
        )  # always >= 1 split for a valid series (it raises on too-few observations)
        positive = sum(1 for sp in splits if _sharpe([returns[i] for i in sp.test]) > 0)
        return positive / len(splits)
