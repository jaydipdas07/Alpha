"""The risk-officer's review — bundling the paper-eval + the one-shot holdout gate (M3.7).

This is the **off-pod** half of Workflow B. Before a candidate reaches the human FORM two
gates must clear, both computed on the research plane where the data physically lives:

1. ``evaluate_paper_run`` — forward paper-trading confirms the backtest (``paper_eval.py``).
2. ``HoldoutGate.evaluate`` — the candidate clears the rigor verdict on the *locked* holdout,
   the **single legitimate read** of the no-ACL holdout store (``holdout_gate.py``, TEST-3).

``RiskOfficerReview`` combines the two verdicts and produces ``request_fields()`` — the JSON-safe
payload for the pod ``approval_requests`` row that triggers the pod FORM. **That method is the
TEST-3 boundary in code:** it emits only the *scalar verdict summaries* (Sharpe figures, the
holdout verdict + reason), **never** the holdout or paper return series, and never any bars. The
``approval_requests`` table it feeds is granted to **no agent** (only the human approver / cockpit
reads it), so holdout-derived numbers never reach the strategist, RAG, ``desk``, or any pod view.

Pure + injected: no pod / lemma / network dependency lives here (kept out of the kernel like every
broker SDK); the off-pod bridge (``scripts/risk_officer_review.py``) does the pod I/O around it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from alpha_core.core.enums import AssetClass
from alpha_core.research.holdout_gate import HoldoutGateResult
from alpha_core.research.paper_eval import PaperEvaluation


def _finite(x: float) -> float | None:
    """A JSON/pod-safe float: ``NaN``/``±inf`` (a degenerate Sharpe) becomes ``None`` so the
    FLOAT column stores a null rather than rejecting the write."""
    return x if math.isfinite(x) else None


@dataclass(frozen=True, slots=True)
class RiskOfficerReview:
    """The combined risk-officer verdict on a candidate, ready to feed the human FORM.

    ``passed`` requires BOTH gates: paper confirmed the backtest AND the candidate promoted on the
    never-seen holdout. A candidate that fails either is not surfaced for approval.
    """

    strategy_name: str
    family: str
    market: AssetClass
    venue: str
    mode: str  # "paper" | "live" — the deployment mode under review
    paper: PaperEvaluation
    holdout: HoldoutGateResult
    origin: str = "discovery"  # "discovery" (a real survivor) | "demo" (a labelled pipeline test)

    @property
    def passed(self) -> bool:
        """Both gates cleared — the only state that should reach the human FORM."""
        return self.paper.passed and self.holdout.passed

    @property
    def gate_status(self) -> str:
        """The off-pod combined verdict, mirrored onto ``approval_requests.gate_status``."""
        return "passed" if self.passed else "rejected"

    def request_fields(self, *, deployment_id: str, strategy_id: str) -> dict[str, object]:
        """The JSON-safe ``approval_requests`` row payload — the TEST-3 boundary.

        Emits ONLY scalar verdict summaries (Sharpe figures, the holdout verdict + its reason) plus
        the strategy identity; it **never** emits the paper or holdout *return series* or any bars,
        so the agent-excluded ``approval_requests`` table can carry the holdout-derived verdict
        without any path by which an agent / RAG / cockpit view could reconstruct the holdout data.
        """
        return {
            "deployment_id": deployment_id,
            "strategy_id": strategy_id,
            "strategy_name": self.strategy_name,
            "family": self.family,
            "market": self.market.value.lower(),  # pod `market` ENUM is lowercase (crypto/equity/…)
            "venue": self.venue,
            "mode": self.mode,
            "gate_status": self.gate_status,
            # paper-eval (statistics plane — floats, never money)
            "paper_sharpe": _finite(self.paper.paper_sharpe),
            "backtest_sharpe": _finite(self.paper.backtest_sharpe),
            "sharpe_retention": _finite(self.paper.sharpe_retention),
            # one-shot holdout gate (HOLDOUT-DERIVED — why this table is agent-excluded, TEST-3)
            "holdout_verdict": self.holdout.verdict.value,
            "holdout_oos_sharpe": _finite(self.holdout.oos_sharpe),
            "holdout_n_obs": self.holdout.n_obs,
            "holdout_reason": self.holdout.reason,
            "origin": self.origin,
            "status": "pending",
            # a compact audit echo — still scalar summaries only, never a return series
            "detail": {
                "paper": {
                    "passed": self.paper.passed,
                    "reason": self.paper.reason,
                    **self.paper.metrics(),
                },
                "holdout": {
                    "passed": self.holdout.passed,
                    "verdict": self.holdout.verdict.value,
                    "oos_sharpe": _finite(self.holdout.oos_sharpe),
                    "n_obs": self.holdout.n_obs,
                    "reason": self.holdout.reason,
                },
            },
        }


def assemble_review(
    *,
    strategy_name: str,
    family: str,
    market: AssetClass,
    venue: str,
    paper: PaperEvaluation,
    holdout: HoldoutGateResult,
    mode: str = "paper",
    origin: str = "discovery",
) -> RiskOfficerReview:
    """Combine the paper-eval and one-shot holdout verdicts into the risk-officer review."""
    return RiskOfficerReview(
        strategy_name=strategy_name,
        family=family,
        market=market,
        venue=venue,
        mode=mode,
        paper=paper,
        holdout=holdout,
        origin=origin,
    )
