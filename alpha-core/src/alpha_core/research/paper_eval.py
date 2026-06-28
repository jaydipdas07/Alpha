"""Paper-run evaluator (M3.7) — does forward paper-trading confirm the backtest?

``evaluate_paper_run`` compares the live paper return series to the strategy's backtest
return series and decides pass/fail: the paper edge must persist (an absolute Sharpe
floor) **and** keep enough of the backtest Sharpe (so slippage / fees / real fills
haven't eaten the edge). A ``passed`` paper run is what DATASTORE-triggers Workflow B
(the deployment-approval FORM). Pure: returns are ``float`` on the statistics plane,
never the money path; the thresholds live in ``config/rigor.yaml`` (no magic numbers).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from alpha_core.backtest.metrics import sharpe
from alpha_core.helpers.config import PaperEvalConfig


@dataclass(frozen=True, slots=True)
class PaperEvaluation:
    """The verdict on a paper run vs its backtest (shaped for ``paper_runs.metrics``)."""

    passed: bool
    paper_sharpe: float
    backtest_sharpe: float
    sharpe_retention: float
    reason: str

    def metrics(self) -> dict[str, float]:
        """JSON-safe divergence metrics for the pod ``paper_runs.metrics`` column."""
        return {
            "paper_sharpe": self.paper_sharpe,
            "backtest_sharpe": self.backtest_sharpe,
            "sharpe_retention": self.sharpe_retention,
        }


def evaluate_paper_run(
    *,
    paper_returns: Sequence[float],
    backtest_returns: Sequence[float],
    config: PaperEvalConfig | None = None,
) -> PaperEvaluation:
    """Pass a paper run iff its live edge persists and keeps enough of the backtest Sharpe.

    ``sharpe_retention`` is paper/backtest Sharpe: how much of the modelled edge survived
    real fills. When the backtest had no positive edge the ratio is undefined, so retention
    is treated as 1.0 if the paper run is itself positive, else 0.0 (and the absolute floor
    still applies)."""
    cfg = config or PaperEvalConfig()
    ps = sharpe(paper_returns)
    bs = sharpe(backtest_returns)
    retention = (ps / bs) if bs > 0 else (1.0 if ps > 0 else 0.0)

    if ps < cfg.min_paper_sharpe:
        return PaperEvaluation(
            False, ps, bs, retention, f"paper Sharpe {ps:.3f} < floor {cfg.min_paper_sharpe}"
        )
    if bs > 0 and retention < cfg.min_sharpe_retention:
        return PaperEvaluation(
            False,
            ps,
            bs,
            retention,
            f"kept only {retention:.0%} of the backtest Sharpe (< {cfg.min_sharpe_retention:.0%})",
        )
    return PaperEvaluation(
        True, ps, bs, retention, f"paper Sharpe {ps:.3f} confirms backtest {bs:.3f}"
    )
