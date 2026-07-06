#!/usr/bin/env python
"""The disciplined F1 sweep: NIFTY noise-area breakout on the index minute cell + one
holdout read per survivor (the intraday-mandate headline family,
`docs/research/intraday-edge-survey-2026-07.md` §5).

The ``seasonality_review`` pattern, scoped to ``F1_TEMPLATES`` (its own registry — the
nightly universe untouched) with three F1-specific pieces of honesty:

- **The session gate (SF4, TEST-1):** both sides of the backtester pair run under
  ``schedule_for({EQUITY})`` + ``intraday_square_off=True`` — the fold cannot enter where
  the live Worker would refuse (Kite minute tapes carry the 15:10+ blocked tail), and the
  book is MIS-flat daily exactly as the deployment would be.
- **The futures cost overlay** (``futures_costed_equity_config``): the cell marks at INDEX
  levels but the deployable is the near-month future — the fold charges the Budget-2026
  futures stack (~6 bps round trip), never the understated cash-equity STT. Intraday
  index-vs-future basis drift stays unmodelled (declared; revisit before paper).
- **Read-timing on window SIZE only** (``--holdout-reads skip``, the FW-driver rule):
  freeze survivors without spending the one-shot read when the sealed window lacks power.

Run AFTER the deep minute ingest lands in the research store and the seal is refreshed
(``scripts/seal_cold_store.py`` — boundaries are floored monotonic)::

    ALPHA_RESEARCH_ROOT=data_research ALPHA_HOLDOUT_ROOT=data_holdout \\
        uv run python scripts/f1_intraday_review.py

The pre-registered space is 4 configs (lookback {14, 90} x conditioning {none, fhh}) on
ONE cell — ``max_attempts=500`` makes "saturated" provably mean "exhausted". Record every
holdout read in TASKS.md.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from alpha_core.core.enums import AssetClass
from alpha_core.data.holdout import HoldoutStore
from alpha_core.data.store import BarStore
from alpha_core.helpers.config import DiscoveryCellConfig, load_discovery_config, load_yaml
from alpha_core.research.cost_scenarios import futures_costed_equity_config
from alpha_core.research.discovery import Backtester
from alpha_core.research.f1_templates import F1_TEMPLATES
from alpha_core.research.holdout_gate import HoldoutGate
from alpha_core.research.promote import build_survivor_backtesters
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.quant_analyst import QuantAnalyst, Verdict, deflation_inputs
from alpha_core.research.strategist import (
    CellSaturated,
    RandomProposer,
    Strategist,
    StrategyProposal,
)
from alpha_core.risk.limits import load_risk_config
from alpha_core.scheduler.clock import schedule_for

_DEFAULT_CELLS = "nifty50-1m"  # the pre-registered universe: the NIFTY 50 index minute cell


def _cell(cells: Sequence[DiscoveryCellConfig], window: str) -> DiscoveryCellConfig:
    for cell in cells:
        if cell.window == window:
            return cell
    raise SystemExit(
        f"cell {window!r} not in config/discovery.yaml; known: {sorted(c.window for c in cells)}"
    )


def _sweep_one(
    template: str,
    *,
    cell: DiscoveryCellConfig,
    n: int,
    strategist: Strategist,
    ledger: ProposalLedger,
    qa: QuantAnalyst,
    in_sample: Backtester,
    holdout_gate: HoldoutGate | None,
) -> tuple[int, int]:
    """Seeded in-sample sweep for one (template, cell), then ONE holdout read per survivor
    (or a frozen no-read close under ``--holdout-reads skip``)."""
    proposals: list[StrategyProposal] = []
    returns: list[Sequence[float]] = []
    for _ in range(n):
        try:
            proposal = strategist.propose(template, market=cell.market, window=cell.window)
        except CellSaturated:
            print(f"  [{template}] cell saturated after {len(proposals)} proposals (exhaustive)")
            break
        proposals.append(proposal)
        returns.append(list(in_sample.run(proposal)))
    family = F1_TEMPLATES[template].family
    n_trials = ledger.count(cell.market, family, cell.window)

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
    for proposal, a in assessments:
        print(
            f"  {dict(proposal.params)}: {a.verdict.name} "
            f"(DSR={a.deflated_sharpe:.3f}, OOS sharpe={a.oos_sharpe:+.4f})"
        )
    survivors = [(p, a) for p, a in assessments if a.verdict is Verdict.PROMOTE]
    print(
        f"=== {template} / {cell.market.value}/{cell.window} === "
        f"{len(proposals)} candidates, n_trials={n_trials}, {len(survivors)} survivor(s); "
        f"DSR variance={variance:.3e}"
    )
    passes = 0
    if holdout_gate is None:
        for proposal, assessment in survivors:
            print(
                f"  survivor {dict(proposal.params)}: in-sample OOS="
                f"{assessment.oos_sharpe:+.4f} -> FROZEN (holdout read deliberately skipped)"
            )
        return len(survivors), 0
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
        description="Seeded F1 noise-breakout sweep + one holdout read per survivor"
    )
    ap.add_argument(
        "--templates",
        default="all",
        help=f"comma-list, or 'all' (registered: {sorted(F1_TEMPLATES)})",
    )
    ap.add_argument("--cells", default=_DEFAULT_CELLS, help="comma-list of discovery.yaml windows")
    ap.add_argument("--seed", type=int, default=10, help="RandomProposer seed (reproducibility)")
    ap.add_argument(
        "--n",
        type=int,
        default=50,
        help="in-sample candidates per (template, cell) — the 4-config space saturates instantly",
    )
    ap.add_argument(
        "--holdout-reads",
        choices=("auto", "skip"),
        default="auto",
        help="'skip' = in-sample only: freeze survivors WITHOUT spending the one-shot holdout "
        "read — for when the sealed window is too thin to power a verdict. Choosing WHEN to "
        "read on window SIZE (power) is legitimate — the candidate is frozen before any "
        "holdout contact either way.",
    )
    args = ap.parse_args()

    templates = (
        sorted(F1_TEMPLATES)
        if args.templates == "all"
        else [t.strip() for t in args.templates.split(",")]
    )
    unknown = [t for t in templates if t not in F1_TEMPLATES]
    if unknown:
        raise SystemExit(f"unknown template(s) {unknown}; registered: {sorted(F1_TEMPLATES)}")

    cells_cfg = load_discovery_config().cells
    cells = [_cell(cells_cfg, window.strip()) for window in args.cells.split(",")]
    research = BarStore(Path(os.environ["ALPHA_RESEARCH_ROOT"]))
    holdout = HoldoutStore(Path(os.environ["ALPHA_HOLDOUT_ROOT"]))
    risk_config = load_risk_config()
    # The futures cost overlay + the live session gate — the family's two honesty pieces.
    cost_config = futures_costed_equity_config(load_yaml("costs.yaml"))
    schedule = schedule_for(frozenset({AssetClass.EQUITY}))

    qa = QuantAnalyst()
    total_survivors = total_passes = 0
    with ProposalLedger() as ledger:
        strategist = Strategist(
            ledger,
            proposer=RandomProposer(seed=args.seed),
            templates=F1_TEMPLATES,
            max_attempts=500,
        )
        for cell in cells:
            in_sample, holdout_backtester = build_survivor_backtesters(
                cell=cell,
                research_store=research,
                holdout_store=holdout,
                risk_config=risk_config,
                cost_config=cost_config,
                templates=F1_TEMPLATES,  # resolver parity with the Strategist
                schedule=schedule,
                intraday_square_off=True,  # MIS-style, both sides (rigor symmetry)
            )
            gate = (
                HoldoutGate(backtester=holdout_backtester, quant_analyst=qa)
                if args.holdout_reads == "auto"
                else None
            )
            if gate is None:
                print(
                    "=== HOLDOUT READS: SKIPPED (in-sample only; survivors frozen, "
                    "no read spent) ==="
                )
            for template in templates:
                survivors, passes = _sweep_one(
                    template,
                    cell=cell,
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
        f"=== F1 verdict: {total_passes}/{total_survivors} survivor(s) PASS the holdout across "
        f"{len(templates)} template(s) x {len(cells)} cell(s) ==="
    )


if __name__ == "__main__":
    main()
