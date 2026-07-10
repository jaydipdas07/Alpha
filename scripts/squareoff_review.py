#!/usr/bin/env python
"""The disciplined G3 sweep: seeded square-off-unwind proposals over the ONE NIFTY-100
panel cell (`docs/research/intraday-edge-survey-2026-07-v2.md` §G3).

Protocol identical to ``scripts/sip_orb_review.py`` (one in-memory ledger, exhaustive
4-config space via ``max_attempts=500``, holdout only through ``HoldoutGate``, the
registered universe file as the single member list, ``--holdout-reads`` defaulting to
``skip`` on the floor-thinned NSE|60 window — the #169/#179 precedent; G3 has no
trailing warmup, but ~20 holdout sessions is still too feeble for a one-shot read).

Run AFTER the universe minute ingest + cold-store seal (`ingest_kite.py` /
`seal_cold_store.py`)::

    ALPHA_RESEARCH_ROOT=data_research ALPHA_HOLDOUT_ROOT=data_holdout \\
        uv run python scripts/squareoff_review.py
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from alpha_core.core.enums import AssetClass
from alpha_core.data.holdout import HoldoutStore
from alpha_core.data.store import BarStore
from alpha_core.research.discovery import Backtester
from alpha_core.research.holdout_gate import HoldoutGate
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.quant_analyst import QuantAnalyst, Verdict, deflation_inputs
from alpha_core.research.squareoff_backtester import (
    SQUAREOFF_CELLS,
    SQUAREOFF_TEMPLATES,
    build_squareoff_backtesters,
)
from alpha_core.research.strategist import (
    CellSaturated,
    RandomProposer,
    Strategist,
    StrategyProposal,
)

_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UNIVERSE = _ROOT / "docs" / "research" / "nifty100-universe-2026-07.txt"


def load_universe(path: Path) -> list[str]:
    """The registered universe: non-comment whitespace-separated store symbols."""
    symbols: list[str] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        symbols.extend(line.split())
    if not symbols:
        raise SystemExit(f"no symbols in universe file {path}")
    return symbols


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
            proposal = strategist.propose(template, market=AssetClass.EQUITY, window=window)
        except CellSaturated:
            print(f"  [{template}] cell saturated after {len(proposals)} proposals (exhaustive)")
            break
        proposals.append(proposal)
        returns.append(in_sample.run(proposal))  # numpy session marks stay numpy
    family = SQUAREOFF_TEMPLATES[template].family
    n_trials = ledger.count(AssetClass.EQUITY, family, window)
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
        f"=== {template} / EQUITY/{window} === {len(proposals)} candidates, "
        f"n_trials={n_trials}, {len(survivors)} survivor(s); DSR variance={variance:.3e}"
    )
    passes = 0
    if holdout_gate is None:
        for p, a in survivors:
            print(
                f"  survivor {dict(p.params)}: in-sample OOS={a.oos_sharpe:+.4f} -> "
                "FROZEN (holdout read deliberately skipped — floor-thinned window)"
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
        description="Seeded G3 square-off unwind sweep (one panel cell, 4 exhaustive configs)"
    )
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--universe-file", type=Path, default=DEFAULT_UNIVERSE)
    ap.add_argument(
        "--holdout-reads",
        choices=("skip", "auto"),
        default="skip",
        help="'skip' (DEFAULT) freezes survivors without spending the read — the NSE|60 "
        "holdout is floor-thinned to ~20 sessions. 'auto' spends one read per PROMOTE — "
        "use only once the window has fattened (~Q4-2026).",
    )
    args = ap.parse_args()

    universe = load_universe(args.universe_file)
    research = BarStore(Path(os.environ["ALPHA_RESEARCH_ROOT"]))
    holdout = HoldoutStore(Path(os.environ["ALPHA_HOLDOUT_ROOT"]))
    in_sample, holdout_bt = build_squareoff_backtesters(
        research_store=research, holdout_store=holdout, universe=universe
    )
    qa = QuantAnalyst()
    read_holdout = args.holdout_reads == "auto"
    if not read_holdout:
        print(
            "=== HOLDOUT READS SKIPPED (default): the floor-thinned NSE|60 window is too "
            "feeble to read — survivors will be FROZEN ==="
        )
    gate = HoldoutGate(backtester=holdout_bt, quant_analyst=qa) if read_holdout else None

    total_survivors = total_passes = 0
    with ProposalLedger() as ledger:
        strategist = Strategist(
            ledger,
            proposer=RandomProposer(seed=args.seed),
            templates=SQUAREOFF_TEMPLATES,
            max_attempts=500,
        )
        for window in sorted(SQUAREOFF_CELLS):
            survivors, passes = _sweep_one(
                "squareoff_unwind",
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
        f"=== G3[cash-MIS taker] verdict: {total_passes}/{total_survivors} survivor(s) "
        f"PASS the holdout across {len(SQUAREOFF_CELLS)} cell(s) ==="
    )


if __name__ == "__main__":
    main()
