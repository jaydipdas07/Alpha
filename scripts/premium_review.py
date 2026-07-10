#!/usr/bin/env python
"""The disciplined G7 sweep: seeded premium-dislocation proposals over the two major
cells + one holdout read per survivor
(`docs/research/intraday-edge-survey-2026-07-v2.md` round-four addendum).

Protocol identical to ``scripts/depth_review.py`` (one in-memory ledger, exhaustive
4-config space per cell via ``max_attempts=500``, holdout only through ``HoldoutGate``
over the pair built in one tested place — here over the research/holdout TICK stores +
the research/holdout PREMIUM stores), with the same two family-specific properties:
maker-native execution is FIXED by the registration (no ``--cost-scenario`` switch;
survivors EARN their read) and one backtester pair PER CELL (one symbol's ~75M-row 1s
arrays resident at a time — the 16 GB Mac's one-heavy-job discipline).

Run AFTER the premium ingest + premium seal (`ingest_binance_premium.py` /
`seal_premium_store.py`)::

    ALPHA_TICK_RESEARCH_ROOT=data_research_ticks ALPHA_TICK_HOLDOUT_ROOT=data_holdout_ticks \\
    ALPHA_PREMIUM_RESEARCH_ROOT=data_research_premium \\
    ALPHA_PREMIUM_HOLDOUT_ROOT=data_holdout_premium \\
        uv run python scripts/premium_review.py
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from alpha_core.core.enums import AssetClass
from alpha_core.data.premium_store import PremiumStore
from alpha_core.data.tick_store import TickStore
from alpha_core.research.discovery import Backtester
from alpha_core.research.holdout_gate import HoldoutGate
from alpha_core.research.premium_backtester import (
    PREMIUM_CELLS,
    PREMIUM_TEMPLATES,
    build_premium_backtesters,
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
        returns.append(in_sample.run(proposal))  # numpy minute marks stay numpy
    family = PREMIUM_TEMPLATES[template].family
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
    ap = argparse.ArgumentParser(
        description="Seeded G7 premium-dislocation sweep + one holdout read per survivor"
    )
    ap.add_argument("--cells", default=",".join(sorted(PREMIUM_CELLS)))
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument(
        "--holdout-reads",
        choices=("auto", "skip"),
        default="auto",
        help="'auto' spends one read per PROMOTE (execution here is deployable: modelled "
        "post-only fills + taker exits); 'skip' freezes survivors without spending the "
        "read (the #169 floor-thinning escape hatch).",
    )
    args = ap.parse_args()

    windows = [w.strip() for w in args.cells.split(",")]
    bad = [w for w in windows if w not in PREMIUM_CELLS]
    if bad:
        raise SystemExit(f"unknown cell(s) {bad}; known: {sorted(PREMIUM_CELLS)}")

    research_ticks = TickStore(Path(os.environ["ALPHA_TICK_RESEARCH_ROOT"]))
    holdout_ticks = TickStore(Path(os.environ["ALPHA_TICK_HOLDOUT_ROOT"]))
    research_premium = PremiumStore(Path(os.environ["ALPHA_PREMIUM_RESEARCH_ROOT"]))
    holdout_premium = PremiumStore(Path(os.environ["ALPHA_PREMIUM_HOLDOUT_ROOT"]))
    qa = QuantAnalyst()
    read_holdout = args.holdout_reads == "auto"
    if not read_holdout:
        print("=== HOLDOUT READS SKIPPED: survivors will be FROZEN (--holdout-reads skip) ===")

    total_survivors = total_passes = 0
    with ProposalLedger() as ledger:
        strategist = Strategist(
            ledger,
            proposer=RandomProposer(seed=args.seed),
            templates=PREMIUM_TEMPLATES,
            max_attempts=500,
        )
        for window in windows:
            # a fresh pair per cell: exactly one symbol's 1s arrays resident at a time
            in_sample, holdout_bt = build_premium_backtesters(
                research_ticks=research_ticks,
                holdout_ticks=holdout_ticks,
                research_premium=research_premium,
                holdout_premium=holdout_premium,
            )
            gate = HoldoutGate(backtester=holdout_bt, quant_analyst=qa) if read_holdout else None
            survivors, passes = _sweep_one(
                "premium_dislocation",
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
        f"=== G7[maker-native] verdict: {total_passes}/{total_survivors} survivor(s) "
        f"PASS the holdout across {len(windows)} cell(s) ==="
    )


if __name__ == "__main__":
    main()
