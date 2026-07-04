"""F4 EXPLORATORY study — cascade reversion on the ARCHIVED (cm, 2023-06→2024-10) era.

*** NO GATE, NO HOLDOUT READ, NO DEPLOYMENT CLAIM CAN COME FROM THIS SCRIPT. ***

Recorder-era pre-registration notes (carry forward): the z is mean-free (``f/sigma``) — under
persistently one-sided flow it reads level, not surprise; and after a fully-quiet trailing
day the FIRST event of any size saturates ``|z| ~ sqrt(n_valid)`` — on the quieter,
downsampled um stream the {4,8} z grid partially collapses after quiet stretches. Both must
be addressed (or explicitly accepted) in the recorder-era family definition.

The archived liquidation series ends 2024-10-14 (before any holdout window) and the
deployable live signal (um forceOrder) is a different, downsampled source — so this run is a
PRIOR CHECK only: does archive-era cascade reversion clear taker costs in-sample at all? The
gate-eligible F4 family runs on recorder-era data later (``scripts/record_liquidations.py``).
In-sample verdict labels below reuse the quant-analyst for comparable numbers; a PROMOTE here
means "worth pre-registering on the recorder era", nothing more.

Run AFTER the tick backfill covers the archive era and ``scripts/seal_tick_store.py``::

    ALPHA_TICK_RESEARCH_ROOT=data_research_ticks ALPHA_LIQ_ROOT=data_liq \\
        uv run python scripts/liquidation_explore.py
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import numpy as np

from alpha_core.core.enums import AssetClass
from alpha_core.data.tick_store import TickStore
from alpha_core.research.liquidation_backtester import (
    LIQ_CELLS,
    LIQ_TEMPLATES,
    build_liquidation_explorer,
)
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.quant_analyst import QuantAnalyst, deflation_inputs
from alpha_core.research.strategist import CellSaturated, RandomProposer, Strategist


def main() -> None:
    ap = argparse.ArgumentParser(description="F4 exploratory archive study (NO GATE)")
    ap.add_argument("--cells", default=",".join(sorted(LIQ_CELLS)))
    ap.add_argument("--seed", type=int, default=10)
    args = ap.parse_args()
    windows = [w.strip() for w in args.cells.split(",")]
    bad = [w for w in windows if w not in LIQ_CELLS]
    if bad:
        raise SystemExit(f"unknown cell(s) {bad}; known: {sorted(LIQ_CELLS)}")

    print("=" * 78)
    print("F4 EXPLORATORY — archived cm era only; NO holdout exists for this data; any")
    print("PROMOTE below means 'pre-register on recorder-era data', never 'deployable'.")
    print("=" * 78)

    ticks = TickStore(Path(os.environ["ALPHA_TICK_RESEARCH_ROOT"]))
    explorer = build_liquidation_explorer(
        research_ticks=ticks, liq_root=Path(os.environ["ALPHA_LIQ_ROOT"])
    )
    qa = QuantAnalyst()
    with ProposalLedger() as ledger:
        strategist = Strategist(
            ledger,
            proposer=RandomProposer(seed=args.seed),
            templates=LIQ_TEMPLATES,
            max_attempts=500,
        )
        for window in windows:
            proposals = []
            returns = []
            while True:
                try:
                    proposals.append(
                        strategist.propose(
                            "liq_cascade_revert", market=AssetClass.CRYPTO, window=window
                        )
                    )
                except CellSaturated:
                    print(f"  [liq_cascade_revert] saturated after {len(proposals)} (exhaustive)")
                    break
                returns.append(explorer.run(proposals[-1]))
            n_trials = ledger.count(
                AssetClass.CRYPTO, LIQ_TEMPLATES["liq_cascade_revert"].family, window
            )
            _, variance = deflation_inputs(returns, oos_fraction=qa.oos_fraction)
            for p, series in zip(proposals, returns, strict=True):
                a = qa.assess(
                    series,
                    n_trials=n_trials,
                    trial_sharpe_variance=variance,
                    oos_fraction=qa.oos_fraction,
                )
                arr = np.asarray(series)
                print(
                    f"  {dict(p.params)}: prior:{a.verdict.name} (DSR={a.deflated_sharpe:.3f}, "
                    f"OOS sharpe={a.oos_sharpe:+.4f}, trades~{int(np.count_nonzero(arr))}, "
                    f"sum={arr.sum():+.4f})"
                )
            print(f"=== exploratory {window}: {len(proposals)} candidates ===")
    print("=== F4 exploratory done — recorder-era pre-registration is the next gate step ===")


if __name__ == "__main__":
    main()
