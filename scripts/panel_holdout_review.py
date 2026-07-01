"""The disciplined cross-sectional re-run: a seeded multi-family panel sweep + one holdout read
per survivor (M3.0 follow-on — the decisive TEST-3 reads).

The panel analogue of ``scripts/risk_officer_review.py``'s survivor path, scoped to the holdout gate
(the paper-eval + the human FORM are post-pass and [You]-gated). Reproducible by design: a *seeded*
in-sample sweep over the **sealed research** store (holdout-free, TEST-3) locks the frozen
survivor(s) and the DSR deflation inputs (the per-family trial count + the cross-trial Sharpe
variance over the in-sample population); then each survivor is read **once** on the gate-only
``HoldoutStore`` (wired by ``promote.build_panel_backtesters`` /
``build_funding_panel_backtesters``) — the single legitimate read.

**One ledger, every family.** All requested templates (price rank + richer book constructions +
funding carry) sweep under ONE in-memory ``ProposalLedger``, so each family's ``n_trials`` is its
honest count for this run. NB this standalone count is optimistic vs the durable nightly ledger (a
durable cell would carry more trials -> a harsher DSR penalty), so a REJECT here is conservative.

**Holdout-read discipline:** re-seal (``scripts/seal_cold_store.py`` — the boundary is floored
monotonic, it can only roll FORWARD) before each new family's holdout read, and record every read in
TASKS.md — the same sealed window must not be quietly re-mined across sessions.

Roots from ``ALPHA_RESEARCH_ROOT`` / ``ALPHA_HOLDOUT_ROOT`` (like ``seal_cold_store.py``), plus
``ALPHA_FUNDING_ROOT`` — required for carry templates (funding IS the signal) and folded into every
perp panel's price P&L when set. Run AFTER seal::

    uv run python scripts/panel_holdout_review.py                 # all registered families
    uv run python scripts/panel_holdout_review.py --templates cross_sectional_momentum

Box runbook (the M3.0-follow-on re-run; the t4g.small hosts the LIVE worker — daily-only, and trim
the nightly timer first): ingest_cold_store + ingest_funding (2019-09 windows) -> seal_cold_store
(verify every daily holdout start prints >= the previous 2025-11-09 boundary) -> this script ->
record the per-family verdicts + reads in TASKS.md.
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
from alpha_core.research.discovery import Backtester
from alpha_core.research.funding_backtester import FUNDING_TEMPLATES
from alpha_core.research.holdout_gate import HoldoutGate
from alpha_core.research.panel_backtester import PANEL_TEMPLATES
from alpha_core.research.promote import build_funding_panel_backtesters, build_panel_backtesters
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.quant_analyst import QuantAnalyst, Verdict, deflation_inputs
from alpha_core.research.strategist import (
    CellSaturated,
    RandomProposer,
    Strategist,
    StrategyProposal,
)

ALL_TEMPLATES = {**PANEL_TEMPLATES, **FUNDING_TEMPLATES}


def _sweep_one_family(
    template: str,
    *,
    market: AssetClass,
    panel: str,
    n: int,
    strategist: Strategist,
    ledger: ProposalLedger,
    qa: QuantAnalyst,
    in_sample: Backtester,
    holdout_gate: HoldoutGate,
) -> tuple[int, int]:
    """Seeded in-sample sweep for one family, then ONE holdout read per survivor. Returns
    ``(survivors, passes)``."""
    proposals: list[StrategyProposal] = []
    returns: list[Sequence[float]] = []
    for _ in range(n):
        try:
            proposal = strategist.propose(template, market=market, window=panel)
        except CellSaturated:
            break
        proposals.append(proposal)
        returns.append(list(in_sample.run(proposal)))
    n_trials = ledger.count(market, ALL_TEMPLATES[template].family, panel)

    # the DSR deflation inputs (this family's trial count + the cross-trial variance over the
    # in-sample population), then the in-sample verdicts — as the discovery cycle adjudicates.
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
        f"=== {template} / {market.value}/{panel} === "
        f"in-sample: {len(proposals)} candidates, n_trials={n_trials}, "
        f"{len(survivors)} survivor(s); DSR variance={variance:.3e}"
    )
    passes = 0
    # THE ONE-SHOT HOLDOUT READ (TEST-3) — each frozen survivor, once, on the gate-only store.
    for proposal, assessment in survivors:
        result = holdout_gate.evaluate(proposal, n_trials=n_trials, trial_sharpe_variance=variance)
        passes += result.passed
        print(
            f"  survivor {dict(proposal.params)}: in-sample OOS={assessment.oos_sharpe:+.4f} -> "
            f"HOLDOUT {'PASS' if result.passed else 'REJECT'} "
            f"(verdict={result.verdict.name}, oos_sharpe={result.oos_sharpe:+.4f}, "
            f"n_obs={result.n_obs}; {result.reason})"
        )
    return len(survivors), passes


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Seeded multi-family panel sweep + one holdout read per survivor"
    )
    ap.add_argument(
        "--templates",
        default="all",
        help=f"comma-list of templates, or 'all' (registered: {sorted(ALL_TEMPLATES)})",
    )
    ap.add_argument("--panel", default="crypto-perps-1d", help="the panel window/name")
    ap.add_argument("--market", default="CRYPTO")
    ap.add_argument("--seed", type=int, default=10, help="RandomProposer seed (reproducibility)")
    ap.add_argument("--n", type=int, default=50, help="in-sample candidates per family")
    args = ap.parse_args()

    templates = sorted(ALL_TEMPLATES) if args.templates == "all" else args.templates.split(",")
    unknown = [t for t in templates if t not in ALL_TEMPLATES]
    if unknown:
        raise SystemExit(f"unknown template(s) {unknown}; registered: {sorted(ALL_TEMPLATES)}")

    market = AssetClass(args.market)
    research = BarStore(Path(os.environ["ALPHA_RESEARCH_ROOT"]))
    holdout = HoldoutStore(Path(os.environ["ALPHA_HOLDOUT_ROOT"]))
    funding_root = os.environ.get("ALPHA_FUNDING_ROOT")
    carry_requested = any(t in FUNDING_TEMPLATES for t in templates)
    if carry_requested and not funding_root:
        raise SystemExit(
            "ALPHA_FUNDING_ROOT is required for carry templates (funding IS the signal)"
        )
    funding = FundingStore(Path(funding_root)) if funding_root else None

    qa = QuantAnalyst()
    # the TEST-3-critical wiring (in-sample <- research store; holdout <- gate-only store) lives in
    # one tested place so a future edit can't silently cross the two. Perp panels fold funding into
    # the price P&L whenever the store is available.
    price_pair = build_panel_backtesters(
        research_store=research, holdout_store=holdout, funding_store=funding
    )
    carry_pair = (
        build_funding_panel_backtesters(
            research_store=research, holdout_store=holdout, funding_store=funding
        )
        if funding is not None
        else None
    )

    total_survivors = total_passes = 0
    # ONE ledger across every family: each family's n_trials is its honest count for this run.
    with ProposalLedger() as ledger:
        strategist = Strategist(
            ledger, proposer=RandomProposer(seed=args.seed), templates=ALL_TEMPLATES
        )
        for template in templates:
            if template in FUNDING_TEMPLATES:
                assert carry_pair is not None  # guarded above: carry requires the funding store
                in_sample, holdout_backtester = carry_pair
            else:
                in_sample, holdout_backtester = price_pair
            gate = HoldoutGate(backtester=holdout_backtester, quant_analyst=qa)
            survivors, passes = _sweep_one_family(
                template,
                market=market,
                panel=args.panel,
                n=args.n,
                strategist=strategist,
                ledger=ledger,
                qa=qa,
                in_sample=in_sample,
                holdout_gate=gate,
            )
            total_survivors += survivors
            total_passes += passes
    print(
        f"=== sweep verdict: {total_passes}/{total_survivors} survivor(s) PASS the holdout "
        f"across {len(templates)} family(ies) ==="
    )


if __name__ == "__main__":
    main()
