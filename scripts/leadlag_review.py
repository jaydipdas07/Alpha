"""The disciplined F3 sweep: seeded lead-lag run over the 1s alt cells + one holdout read per
survivor (`docs/research/intraday-edge-survey-2026-07.md` §2.3/§5).

Protocol identical to ``scripts/funding_window_review.py`` (one in-memory ledger, exhaustive
8-config space per cell via ``max_attempts=500``, holdout only through ``HoldoutGate`` over
the pair built in one tested place — here over the research/holdout TICK stores). Run AFTER
``scripts/ingest_binance_ticks.py`` and ``scripts/seal_tick_store.py``::

    ALPHA_TICK_RESEARCH_ROOT=data_research_ticks ALPHA_TICK_HOLDOUT_ROOT=data_holdout_ticks \\
        uv run python scripts/leadlag_review.py
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from alpha_core.core.enums import AssetClass
from alpha_core.data.tick_store import TickStore
from alpha_core.research.discovery import Backtester
from alpha_core.research.holdout_gate import HoldoutGate
from alpha_core.research.leadlag_backtester import (
    LEADLAG_CELLS,
    LEADLAG_TEMPLATES,
    build_leadlag_backtesters,
)
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
    holdout_gate: HoldoutGate,
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
        returns.append(in_sample.run(proposal))  # numpy minute-marks stay numpy
    family = LEADLAG_TEMPLATES[template].family
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
    ap = argparse.ArgumentParser(description="Seeded F3 sweep + one holdout read per survivor")
    ap.add_argument("--cells", default=",".join(sorted(LEADLAG_CELLS)))
    ap.add_argument("--seed", type=int, default=10)
    ap.add_argument("--n", type=int, default=50)
    args = ap.parse_args()

    windows = [w.strip() for w in args.cells.split(",")]
    bad = [w for w in windows if w not in LEADLAG_CELLS]
    if bad:
        raise SystemExit(f"unknown cell(s) {bad}; known: {sorted(LEADLAG_CELLS)}")

    research = TickStore(Path(os.environ["ALPHA_TICK_RESEARCH_ROOT"]))
    holdout = TickStore(Path(os.environ["ALPHA_TICK_HOLDOUT_ROOT"]))
    in_sample, holdout_bt = build_leadlag_backtesters(
        research_ticks=research, holdout_ticks=holdout
    )
    qa = QuantAnalyst()
    gate = HoldoutGate(backtester=holdout_bt, quant_analyst=qa)

    total_survivors = total_passes = 0
    with ProposalLedger() as ledger:
        strategist = Strategist(
            ledger,
            proposer=RandomProposer(seed=args.seed),
            templates=LEADLAG_TEMPLATES,
            max_attempts=500,
        )
        for window in windows:
            survivors, passes = _sweep_one(
                "leadlag_follow",
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
        f"=== F3 verdict: {total_passes}/{total_survivors} survivor(s) PASS the holdout across "
        f"{len(windows)} cell(s) ==="
    )


if __name__ == "__main__":
    main()
