"""The disciplined F2 seasonality sweep: a seeded in-sample run over the hourly cells + one
holdout read per survivor (the intraday-mandate family,
`docs/research/intraday-edge-survey-2026-07.md` §5).

The single-instrument analogue of ``scripts/panel_holdout_review.py``, scoped to the
``SEASONAL_TEMPLATES`` registry (its own registry — the nightly universe is untouched). For each
requested template x hourly cell it runs a *seeded* in-sample sweep over the **sealed research**
store (holdout-free, TEST-3) via the production ``EngineBacktester`` pair
(``build_survivor_backtesters``), locks the frozen survivor(s) + the DSR deflation inputs, then
reads each survivor **once** on the gate-only ``HoldoutStore`` — the single legitimate read.

**One ledger, every family:** all templates sweep under ONE in-memory ``ProposalLedger``, so each
family's ``n_trials`` is its honest count for this run (standalone-optimistic vs a durable ledger,
so a REJECT here is conservative). **Holdout-read discipline:** re-seal
(``scripts/seal_cold_store.py`` — boundaries are floored monotonic) before the run, and record
every read in TASKS.md.

Run AFTER ingest (1h span 2019-09+) and seal::

    ALPHA_RESEARCH_ROOT=data_research ALPHA_HOLDOUT_ROOT=data_holdout \\
        uv run python scripts/seasonality_review.py
    ... --templates seasonal_hour_long --cells btcusdt-1h
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from alpha_core.data.holdout import HoldoutStore
from alpha_core.data.store import BarStore
from alpha_core.helpers.config import DiscoveryCellConfig, load_discovery_config, load_yaml
from alpha_core.research.cost_scenarios import scenario_cost_config
from alpha_core.research.discovery import Backtester
from alpha_core.research.holdout_gate import HoldoutGate
from alpha_core.research.promote import build_survivor_backtesters
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.quant_analyst import QuantAnalyst, Verdict, deflation_inputs
from alpha_core.research.seasonal_templates import SEASONAL_TEMPLATES
from alpha_core.research.strategist import (
    CellSaturated,
    RandomProposer,
    Strategist,
    StrategyProposal,
)
from alpha_core.risk.limits import load_risk_config

_DEFAULT_CELLS = "btcusdt-1h,ethusdt-1h"  # the pre-registered universe: BTC + ETH hourly


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
    — or, with ``holdout_gate=None`` (the maker scenario / an unpowered window), a frozen
    no-read close. Returns ``(survivors, passes)``."""
    proposals: list[StrategyProposal] = []
    returns: list[Sequence[float]] = []
    for _ in range(n):
        try:
            proposal = strategist.propose(template, market=cell.market, window=cell.window)
        except CellSaturated:
            # visible by design: stopping before n means the pre-registered space is exhausted.
            print(f"  [{template}] cell saturated after {len(proposals)} proposals (exhaustive)")
            break
        proposals.append(proposal)
        returns.append(list(in_sample.run(proposal)))
    family = SEASONAL_TEMPLATES[template].family
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
        description="Seeded F2 seasonality sweep + one holdout read per survivor"
    )
    ap.add_argument(
        "--templates",
        default="all",
        help=f"comma-list, or 'all' (registered: {sorted(SEASONAL_TEMPLATES)})",
    )
    ap.add_argument("--cells", default=_DEFAULT_CELLS, help="comma-list of discovery.yaml windows")
    ap.add_argument("--seed", type=int, default=10, help="RandomProposer seed (reproducibility)")
    ap.add_argument(
        "--n",
        type=int,
        default=50,
        help="in-sample candidates per (template, cell) — the tiny spaces saturate well below it",
    )
    ap.add_argument(
        "--cost-scenario",
        choices=("taker", "maker"),
        default="taker",
        help="execution assumption (cost_scenarios): 'maker' reprices the engine at the "
        "post-only maker fee with no crossing slippage — IN-SAMPLE-ONLY evidence about a "
        "Phase-4+ maker deployment (no post-only fill model exists yet), so maker FORCES "
        "frozen survivors: the holdout is never read under an undeployable assumption.",
    )
    args = ap.parse_args()

    templates = (
        sorted(SEASONAL_TEMPLATES)
        if args.templates == "all"
        else [t.strip() for t in args.templates.split(",")]
    )
    unknown = [t for t in templates if t not in SEASONAL_TEMPLATES]
    if unknown:
        raise SystemExit(f"unknown template(s) {unknown}; registered: {sorted(SEASONAL_TEMPLATES)}")

    cells_cfg = load_discovery_config().cells
    cells = [_cell(cells_cfg, window.strip()) for window in args.cells.split(",")]
    research = BarStore(Path(os.environ["ALPHA_RESEARCH_ROOT"]))
    holdout = HoldoutStore(Path(os.environ["ALPHA_HOLDOUT_ROOT"]))
    risk_config = load_risk_config()
    cost_config = scenario_cost_config(load_yaml("costs.yaml"), args.cost_scenario)
    read_holdout = args.cost_scenario == "taker"
    if not read_holdout:
        print(
            "=== MAKER SCENARIO: fees-only post-only pricing; fill risk unmodelled — "
            "IN-SAMPLE-ONLY evidence; survivors are FROZEN, the holdout is never read ==="
        )

    qa = QuantAnalyst()
    total_survivors = total_passes = 0
    # ONE ledger across every (template, cell): each family's n_trials is its honest run count.
    with ProposalLedger() as ledger:
        # max_attempts=500 (the panel_holdout_review discipline): a 9-config space at the default
        # 50 attempts leaves a (8/9)^50 ≈ 0.3% miss — 500 makes a missed config astronomically
        # unlikely, so "saturated" provably means "exhausted".
        strategist = Strategist(
            ledger,
            proposer=RandomProposer(seed=args.seed),
            templates=SEASONAL_TEMPLATES,
            max_attempts=500,
        )
        for cell in cells:
            in_sample, holdout_backtester = build_survivor_backtesters(
                cell=cell,
                research_store=research,
                holdout_store=holdout,
                risk_config=risk_config,
                cost_config=cost_config,
                # the SAME registry the Strategist proposes from — resolver parity
                templates=SEASONAL_TEMPLATES,
            )
            gate = (
                HoldoutGate(backtester=holdout_backtester, quant_analyst=qa)
                if read_holdout
                else None
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
        f"=== F2[{args.cost_scenario}] verdict: {total_passes}/{total_survivors} survivor(s) "
        f"PASS the holdout across {len(templates)} template(s) x {len(cells)} cell(s) ==="
    )


if __name__ == "__main__":
    main()
