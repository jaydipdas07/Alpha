"""Run a cross-sectional **panel** survivor through the one-shot holdout gate (M3.0 — the decisive
TEST-3 read).

The panel analogue of ``scripts/risk_officer_review.py``'s survivor path, scoped to the holdout gate
(the paper-eval + the human FORM are post-pass and [You]-gated). Reproducible by design: a *seeded*
in-sample sweep over the **sealed research** store (holdout-free, TEST-3) locks the frozen
survivor(s) and the DSR deflation inputs (the trial count + the cross-trial Sharpe variance over the
in-sample population); then each survivor is read **once** on the gate-only ``HoldoutStore`` via
``HoldoutPanelBarsFor`` (wired by ``promote.build_panel_backtesters``) — the single legitimate read.

``n_trials`` here is **this seeded sweep's** count, from a fresh in-memory ledger — a self-contained
search, NOT the cell's durable cumulative count across nightly runs (which ``promote`` reads for the
production single-instrument path). A durable cell would carry more trials → a harsher DSR penalty,
so this standalone read is, if anything, optimistic toward PROMOTE — a REJECT here is conservative.

The sweep is inlined (rather than calling ``run_discovery_cycle``) only so the runner can keep the
in-sample population to hand the holdout gate the same deflation inputs; the propose -> backtest ->
assess steps are identical to the discovery cycle.

Roots from ``ALPHA_RESEARCH_ROOT`` / ``ALPHA_HOLDOUT_ROOT`` (like ``seal_cold_store.py``), plus
``ALPHA_FUNDING_ROOT`` (optional but set it for perp panels — it folds the funding transfer into
both the in-sample and holdout books). Run AFTER seal::

    uv run python scripts/panel_holdout_review.py --template cross_sectional_momentum
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from alpha_core.core.enums import AssetClass
from alpha_core.data.funding import FundingStore
from alpha_core.data.holdout import HoldoutStore
from alpha_core.data.store import BarStore
from alpha_core.research.holdout_gate import HoldoutGate
from alpha_core.research.panel_backtester import PANEL_TEMPLATES
from alpha_core.research.promote import build_panel_backtesters
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.quant_analyst import QuantAnalyst, Verdict, deflation_inputs
from alpha_core.research.strategist import (
    CellSaturated,
    RandomProposer,
    Strategist,
    StrategyProposal,
)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Run a panel survivor through the one-shot holdout gate"
    )
    ap.add_argument(
        "--template", default="cross_sectional_momentum", choices=sorted(PANEL_TEMPLATES)
    )
    ap.add_argument("--panel", default="crypto-perps-1d", help="the panel window/name")
    ap.add_argument("--market", default="CRYPTO")
    ap.add_argument("--seed", type=int, default=10, help="RandomProposer seed (reproducibility)")
    ap.add_argument("--n", type=int, default=50, help="in-sample candidates to propose")
    args = ap.parse_args()

    market = AssetClass(args.market)
    research = BarStore(Path(os.environ["ALPHA_RESEARCH_ROOT"]))
    holdout = HoldoutStore(Path(os.environ["ALPHA_HOLDOUT_ROOT"]))
    # perp panels pay/earn funding continuously — wire it into BOTH folds when the store exists
    # (each fold's price timeline gates which funding days it touches; see build_panel_backtesters).
    funding_root = os.environ.get("ALPHA_FUNDING_ROOT")
    funding = FundingStore(Path(funding_root)) if funding_root else None
    family = PANEL_TEMPLATES[args.template].family
    qa = QuantAnalyst()
    # the TEST-3-critical wiring (in-sample <- research store; holdout <- gate-only store) lives in
    # one tested place so a future edit can't silently cross the two.
    in_sample, holdout_backtester = build_panel_backtesters(
        research_store=research, holdout_store=holdout, funding_store=funding
    )

    # 1. in-sample sweep over the sealed research store (holdout untouched) — keep the population.
    proposals: list[StrategyProposal] = []
    returns: list[Sequence[float]] = []
    with ProposalLedger() as ledger:
        strategist = Strategist(
            ledger, proposer=RandomProposer(seed=args.seed), templates=PANEL_TEMPLATES
        )
        for _ in range(args.n):
            try:
                proposal = strategist.propose(args.template, market=market, window=args.panel)
            except CellSaturated:
                break
            proposals.append(proposal)
            returns.append(list(in_sample.run(proposal)))
        n_trials = ledger.count(market, family, args.panel)

    # 2. the DSR deflation inputs (this sweep's trial count + the cross-trial variance over the
    #    in-sample population), then the in-sample verdicts — as the discovery cycle adjudicates.
    _, variance = deflation_inputs(returns, oos_fraction=qa.oos_fraction)
    assessments = [
        (
            proposal,
            qa.assess(
                series,
                n_trials=n_trials,
                trial_sharpe_variance=variance,
                oos_fraction=qa.oos_fraction,
            ),
        )
        for proposal, series in zip(proposals, returns, strict=True)
    ]
    survivors = [(p, a) for p, a in assessments if a.verdict is Verdict.PROMOTE]
    print(
        f"=== {args.template} / {market.value}/{args.panel} === "
        f"in-sample: {len(proposals)} candidates, n_trials={n_trials}, "
        f"{len(survivors)} survivor(s); DSR variance={variance:.3e}"
    )
    if not survivors:
        print("no in-sample survivor — nothing to read the holdout for.")
        return

    # 3. THE ONE-SHOT HOLDOUT READ (TEST-3) — each frozen survivor, once, on the gate-only store.
    gate = HoldoutGate(backtester=holdout_backtester, quant_analyst=qa)
    passes = 0
    for proposal, assessment in survivors:
        result = gate.evaluate(proposal, n_trials=n_trials, trial_sharpe_variance=variance)
        passes += result.passed
        print(
            f"  survivor {dict(proposal.params)}: in-sample OOS={assessment.oos_sharpe:+.4f} -> "
            f"HOLDOUT {'PASS' if result.passed else 'REJECT'} "
            f"(verdict={result.verdict.name}, oos_sharpe={result.oos_sharpe:+.4f}, "
            f"n_obs={result.n_obs}; {result.reason})"
        )
    print(f"=== holdout verdict: {passes}/{len(survivors)} survivor(s) PASS ===")


if __name__ == "__main__":
    main()
