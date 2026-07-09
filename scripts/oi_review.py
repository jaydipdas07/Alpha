"""The disciplined G5 sweep: seeded OI-flush reversion over the 1h major cells + one
holdout read per survivor (`docs/research/intraday-edge-survey-2026-07-v2.md` §G5).

Protocol identical to ``scripts/flow_review.py`` (one in-memory ledger, exhaustive 8-config
space per cell via ``max_attempts=500``, holdout only through ``HoldoutGate`` over the pair
built in one tested place — here over the research/holdout BAR stores + the research/holdout
METRICS stores). Run AFTER the metrics ingest + metrics seal
(`ingest_binance_metrics.py` / `seal_metrics_store.py`)::

    ALPHA_RESEARCH_ROOT=data_research ALPHA_HOLDOUT_ROOT=data_holdout \\
    ALPHA_METRICS_RESEARCH_ROOT=data_research_metrics \\
    ALPHA_METRICS_HOLDOUT_ROOT=data_holdout_metrics \\
        uv run python scripts/oi_review.py
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from alpha_core.core.enums import AssetClass
from alpha_core.data.holdout import HoldoutStore
from alpha_core.data.metrics_store import MetricsStore
from alpha_core.data.store import BarStore
from alpha_core.research.discovery import Backtester
from alpha_core.research.holdout_gate import HoldoutGate
from alpha_core.research.oi_backtester import OI_CELLS, OI_TEMPLATES, build_oi_backtesters
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.quant_analyst import QuantAnalyst, Verdict, deflation_inputs
from alpha_core.research.strategist import (
    CellSaturated,
    RandomProposer,
    Strategist,
    StrategyProposal,
)


def _sweep_one(
    template: str,
    *,
    window: str,
    n: int,
    strategist: Strategist,
    ledger: ProposalLedger,
    qa: QuantAnalyst,
    in_sample: Backtester,
    holdout_gate: HoldoutGate | None,
) -> tuple[int, int]:
    proposals: list[StrategyProposal] = []
    returns: list[Sequence[float]] = []
    for _ in range(n):
        try:
            proposal = strategist.propose(template, market=AssetClass.CRYPTO, window=window)
        except CellSaturated:
            print(f"  [{template}] cell saturated after {len(proposals)} proposals (exhaustive)")
            break
        proposals.append(proposal)
        returns.append(in_sample.run(proposal))  # numpy hourly marks stay numpy
    family = OI_TEMPLATES[template].family
    n_trials = ledger.count(AssetClass.CRYPTO, family, window)
    _, variance = deflation_inputs(returns, oos_fraction=qa.oos_fraction)
    assessments = [
        (
            p,
            qa.assess(
                series,
                n_trials=n_trials,
                trial_sharpe_variance=variance,
                oos_fraction=qa.oos_fraction,
            ),
        )
        for p, series in zip(proposals, returns, strict=True)
    ]
    for p, a in assessments:
        print(
            f"  {dict(p.params)}: {a.verdict.name} "
            f"(DSR={a.deflated_sharpe:.3f}, OOS sharpe={a.oos_sharpe:+.4f})"
        )
    survivors = [(p, a) for p, a in assessments if a.verdict is Verdict.PROMOTE]
    print(
        f"=== {template} / CRYPTO/{window} === {len(proposals)} candidates, "
        f"n_trials={n_trials}, {len(survivors)} survivor(s); DSR variance={variance:.3e}"
    )
    passes = 0
    if holdout_gate is None:
        for p, a in survivors:
            print(
                f"  survivor {dict(p.params)}: in-sample OOS={a.oos_sharpe:+.4f} -> "
                "FROZEN (holdout read deliberately skipped)"
            )
        return len(survivors), 0
    for p, a in survivors:  # THE ONE-SHOT HOLDOUT READ (TEST-3)
        result = holdout_gate.evaluate(p, n_trials=n_trials, trial_sharpe_variance=variance)
        passes += result.passed
        print(
            f"  survivor {dict(p.params)}: in-sample OOS={a.oos_sharpe:+.4f} -> "
            f"HOLDOUT {'PASS' if result.passed else 'REJECT'} "
            f"(verdict={result.verdict.name}, oos_sharpe={result.oos_sharpe:+.4f}, "
            f"n_obs={result.n_obs}; {result.reason})"
        )
    return len(survivors), passes


def main() -> None:
    ap = argparse.ArgumentParser(description="Seeded G5 OI sweep + one holdout read per survivor")
    ap.add_argument("--cells", default=",".join(sorted(OI_CELLS)))
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument(
        "--cost-scenario",
        choices=("taker", "maker"),
        default="taker",
        help="execution assumption (cost_scenarios.cost_per_side): 'maker' = post-only fees "
        "only (2bps/side vs taker 8) — IN-SAMPLE-ONLY evidence about a Phase-4+ maker "
        "deployment; maker FORCES frozen survivors (the holdout is never read under an "
        "undeployable assumption).",
    )
    ap.add_argument(
        "--holdout-reads",
        choices=("auto", "skip"),
        default="auto",
        help="'skip' freezes survivors without spending the read (the #169 floor-thinning "
        "escape hatch); ignored under maker, which always freezes.",
    )
    args = ap.parse_args()

    windows = [w.strip() for w in args.cells.split(",")]
    bad = [w for w in windows if w not in OI_CELLS]
    if bad:
        raise SystemExit(f"unknown cell(s) {bad}; known: {sorted(OI_CELLS)}")

    research = BarStore(Path(os.environ["ALPHA_RESEARCH_ROOT"]))
    holdout = HoldoutStore(Path(os.environ["ALPHA_HOLDOUT_ROOT"]))
    research_metrics = MetricsStore(Path(os.environ["ALPHA_METRICS_RESEARCH_ROOT"]))
    holdout_metrics = MetricsStore(Path(os.environ["ALPHA_METRICS_HOLDOUT_ROOT"]))
    in_sample, holdout_bt = build_oi_backtesters(
        research_store=research,
        holdout_store=holdout,
        research_metrics=research_metrics,
        holdout_metrics=holdout_metrics,
        cost_scenario=args.cost_scenario,
    )
    qa = QuantAnalyst()
    read_holdout = args.cost_scenario == "taker" and args.holdout_reads == "auto"
    if args.cost_scenario == "maker":
        print(
            "=== MAKER SCENARIO: fees-only post-only pricing; fill risk unmodelled — "
            "IN-SAMPLE-ONLY evidence; survivors are FROZEN, the holdout is never read ==="
        )
    elif not read_holdout:
        print("=== HOLDOUT READS SKIPPED: survivors will be FROZEN (--holdout-reads skip) ===")
    gate = HoldoutGate(backtester=holdout_bt, quant_analyst=qa) if read_holdout else None

    total_survivors = total_passes = 0
    with ProposalLedger() as ledger:
        strategist = Strategist(
            ledger,
            proposer=RandomProposer(seed=args.seed),
            templates=OI_TEMPLATES,
            max_attempts=500,
        )
        for window in windows:
            survivors, passes = _sweep_one(
                "oi_flush_reversion",
                window=window,
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
        f"=== G5[{args.cost_scenario}] verdict: {total_passes}/{total_survivors} survivor(s) "
        f"PASS the holdout across {len(windows)} cell(s) ==="
    )


if __name__ == "__main__":
    main()
