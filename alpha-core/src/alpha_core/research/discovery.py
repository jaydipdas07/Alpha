"""Workflow A — the discovery cycle (B1b.3b) — wire the strategist and the quant-analyst into a
loop that turns a search cell into promote / reject / revise decisions.

One cycle, for a single ``(market, family, window)`` cell:

1. the **strategist** (B1b.1) proposes ``n_candidates`` valid, original configs (each recorded in
   the proposal ledger — durable originality + the trial count);
2. each is **backtested** on in-sample data (holdout untouched — TEST-3) into a per-bar return
   series — *injected* here via the ``Backtester`` protocol, so the loop stays pure and testable;
   the production backtester builds the strategy from the template and runs the engine on
   cold-store in-sample bars;
3. the DSR **deflation inputs** (R4): the cell's **cumulative** trial count (each proposal carries
   its ledger-recorded ``trial_index``, so a second cycle over a durable cell can't under-penalize)
   + the cross-trial Sharpe variance over this run's trials;
4. the **quant-analyst** (B1b.2) adjudicates each candidate against that deflation → a verdict;
5. the promoted candidates are the cycle's **survivors**.

Scope: the deflation **count** is the cell's cumulative trial count (safe across re-runs), but the
**variance** is over this run's trials only — carrying the cross-run cumulative variance
(persisting each trial's score in the ledger) and wiring the real cold-store/engine backtester
(which must also guarantee ``>= 2*n_groups`` bars per candidate and inject ``now``) are the next
increments.

Research-plane only; the loop never touches the holdout (the strategist is data-free and the
backtester is handed in-sample bars only).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from alpha_core.core.enums import AssetClass
from alpha_core.research.quant_analyst import (
    Assessment,
    QuantAnalyst,
    Verdict,
    deflation_inputs,
)
from alpha_core.research.strategist import CellSaturated, Strategist, StrategyProposal


class Backtester(Protocol):
    """Runs a proposal's strategy through a full-rigor in-sample backtest, returning its per-bar
    return series. The production implementation builds the strategy from the proposal's template
    and runs the engine on cold-store in-sample bars (holdout untouched); it is injected so the
    discovery loop is pure and testable."""

    def run(self, proposal: StrategyProposal) -> Sequence[float]: ...


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    """The outcome of one discovery cycle for a cell."""

    template: str
    market: AssetClass
    window: str
    assessments: list[tuple[StrategyProposal, Assessment]]  # every candidate + its verdict
    survivors: list[StrategyProposal]  # the promoted candidates


def run_discovery_cycle(
    template_name: str,
    *,
    market: AssetClass,
    window: str,
    strategist: Strategist,
    quant_analyst: QuantAnalyst,
    backtester: Backtester,
    n_candidates: int,
) -> DiscoveryReport:
    """Run one discovery cycle over the ``(market, template_name, window)`` cell and return the
    promote/reject/revise verdicts + the survivors. Proposes up to ``n_candidates`` originals
    (stopping early if the cell saturates), backtests each in-sample, then adjudicates them all
    against the cell's DSR deflation. An unknown template / invalid cell raises (a caller bug); a
    saturated cell stops the loop and adjudicates what was found."""
    oos_fraction = quant_analyst.oos_fraction  # one source -> deflation + assess share the slice

    # 1-2. propose + backtest until n_candidates originals, or the cell saturates. Only a saturated
    # cell is swallowed; unknown-template / invalid-cell errors propagate (caller bugs).
    trials: list[tuple[StrategyProposal, list[float]]] = []
    for _ in range(n_candidates):
        try:
            proposal = strategist.propose(template_name, market=market, window=window)
        except CellSaturated:
            break  # the cell's bounded space is exhausted — adjudicate what we have
        trials.append((proposal, list(backtester.run(proposal))))

    if not trials:
        return DiscoveryReport(template_name, market, window, [], [])

    # 3. the DSR deflation inputs (R4): the cell's CUMULATIVE trial count (each proposal carries its
    # ledger-recorded trial_index, so a re-run over a durable cell can't under-penalize) + the
    # cross-trial Sharpe variance over THIS run's trials (the cumulative variance is the next
    # increment); 4. adjudicate each candidate against that deflation.
    n_trials = max(proposal.trial_index for proposal, _ in trials)
    _, variance = deflation_inputs([returns for _, returns in trials], oos_fraction=oos_fraction)
    assessments = [
        (
            proposal,
            quant_analyst.assess(
                returns,
                n_trials=n_trials,
                trial_sharpe_variance=variance,
                oos_fraction=oos_fraction,
            ),
        )
        for proposal, returns in trials
    ]

    # 5. survivors = the promoted candidates.
    survivors = [proposal for proposal, a in assessments if a.verdict is Verdict.PROMOTE]
    return DiscoveryReport(template_name, market, window, assessments, survivors)
