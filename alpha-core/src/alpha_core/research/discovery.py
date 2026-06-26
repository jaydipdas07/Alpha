"""Workflow A — the discovery cycle (B1b.3b) — wire the strategist and the quant-analyst into a
loop that turns a search cell into promote / reject / revise decisions.

One cycle, for a single ``(market, family, window)`` cell:

1. the **strategist** (B1b.1) proposes ``n_candidates`` valid, original configs (each recorded in
   the proposal ledger — durable originality + the trial count);
2. each is **backtested** on in-sample data (holdout untouched — TEST-3) into a per-bar return
   series — *injected* here via the ``Backtester`` protocol, so the loop stays pure and testable;
   the production backtester builds the strategy from the template and runs the engine on
   cold-store in-sample bars;
3. the cell's trial returns set the DSR **deflation inputs** (the cumulative trial count + the
   cross-trial Sharpe variance, R4) — computed by ``deflation_inputs`` over this run's trials;
4. the **quant-analyst** (B1b.2) adjudicates each candidate against that deflation → a verdict;
5. the promoted candidates are the cycle's **survivors**.

Scope: this deflates by *this run's* trials, which equals the cell's cumulative count on a fresh
cell (the Done-when case). Carrying the cross-run cumulative variance (persisting each trial's
score in the ledger) and wiring the real cold-store/engine backtester are the next increments.

Research-plane only; the loop never touches the holdout (the strategist is data-free and the
backtester is handed in-sample bars only).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from alpha_core.core.enums import AssetClass
from alpha_core.helpers.config import load_rigor_config
from alpha_core.research.quant_analyst import (
    Assessment,
    QuantAnalyst,
    Verdict,
    deflation_inputs,
)
from alpha_core.research.strategist import Strategist, StrategistError, StrategyProposal


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
    against the cell's DSR deflation."""
    oos_fraction = load_rigor_config().quant_analyst.oos_fraction

    # 1-2. propose + backtest, until n_candidates originals or the cell saturates.
    trials: list[tuple[StrategyProposal, list[float]]] = []
    for _ in range(n_candidates):
        try:
            proposal = strategist.propose(template_name, market=market, window=window)
        except StrategistError:
            break  # the cell's bounded space is exhausted — adjudicate what we have
        trials.append((proposal, list(backtester.run(proposal))))

    if not trials:
        return DiscoveryReport(template_name, market, window, [], [])

    # 3. the cell's deflation inputs (trial count + cross-trial Sharpe variance, R4); 4. adjudicate.
    n_trials, variance = deflation_inputs(
        [returns for _, returns in trials], oos_fraction=oos_fraction
    )
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
