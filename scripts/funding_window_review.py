"""The disciplined FW sweep: seeded funding-window run over the fresh-perp 1m cells + one
holdout read per survivor (`docs/research/intraday-edge-survey-2026-07.md` §2.4/§5).

Same protocol as ``scripts/seasonality_review.py`` (one in-memory ledger, exhaustive tiny
spaces via ``max_attempts=500``, holdout only through ``HoldoutGate`` over the pair built in
one tested place). Funding is the SIGNAL — ``ALPHA_FUNDING_ROOT`` is required. Run AFTER the
1m bulk ingest + ``scripts/seal_cold_store.py`` (fresh 1m series ⇒ fraction-based boundaries;
the majors stay excluded — their 1m floors are burned to ~2026-06 by the M3.0 sweeps)::

    ALPHA_RESEARCH_ROOT=data_research ALPHA_HOLDOUT_ROOT=data_holdout \\
    ALPHA_FUNDING_ROOT=data_funding \\
        uv run python scripts/funding_window_review.py
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
from alpha_core.research.funding_window_backtester import (
    FUNDING_WINDOW_CELLS,
    FUNDING_WINDOW_TEMPLATES,
    build_funding_window_backtesters,
)
from alpha_core.research.holdout_gate import HoldoutGate
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
        returns.append(in_sample.run(proposal))  # numpy stays numpy — 1.6M-bar series
    family = FUNDING_WINDOW_TEMPLATES[template].family
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
                f"  FROZEN survivor (no read spent) {dict(p.params)}: "
                f"in-sample OOS={a.oos_sharpe:+.4f}, n_trials={n_trials}, "
                f"variance={variance:.3e} — record in TASKS.md; read when the window has power"
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
    ap = argparse.ArgumentParser(description="Seeded FW sweep + one holdout read per survivor")
    ap.add_argument("--templates", default="all")
    ap.add_argument("--cells", default=",".join(sorted(FUNDING_WINDOW_CELLS)))
    ap.add_argument("--seed", type=int, default=10)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument(
        "--holdout-reads",
        choices=("auto", "skip"),
        default="auto",
        help="'skip' = in-sample only: freeze survivors WITHOUT spending the one-shot holdout "
        "read — for when the current holdout window is too thin to power a verdict (the 1m "
        "interval floor at 2026-06-24 leaves ~6 days today; it fattens as the span rolls "
        "forward). Choosing WHEN to read on window SIZE (power) is legitimate — the candidate "
        "is frozen before any holdout contact either way.",
    )
    args = ap.parse_args()

    templates = (
        sorted(FUNDING_WINDOW_TEMPLATES)
        if args.templates == "all"
        else [t.strip() for t in args.templates.split(",")]
    )
    unknown = [t for t in templates if t not in FUNDING_WINDOW_TEMPLATES]
    if unknown:
        raise SystemExit(f"unknown template(s) {unknown}")
    windows = [w.strip() for w in args.cells.split(",")]
    bad = [w for w in windows if w not in FUNDING_WINDOW_CELLS]
    if bad:
        raise SystemExit(f"unknown cell(s) {bad}; known: {sorted(FUNDING_WINDOW_CELLS)}")

    research = BarStore(Path(os.environ["ALPHA_RESEARCH_ROOT"]))
    holdout = HoldoutStore(Path(os.environ["ALPHA_HOLDOUT_ROOT"]))
    funding = FundingStore(Path(os.environ["ALPHA_FUNDING_ROOT"]))
    in_sample, holdout_bt = build_funding_window_backtesters(
        research_store=research, holdout_store=holdout, funding_store=funding
    )
    qa = QuantAnalyst()
    gate = (
        HoldoutGate(backtester=holdout_bt, quant_analyst=qa)
        if args.holdout_reads == "auto"
        else None
    )
    if gate is None:
        print("=== HOLDOUT READS: SKIPPED (in-sample only; survivors frozen, no read spent) ===")

    total_survivors = total_passes = 0
    with ProposalLedger() as ledger:
        strategist = Strategist(
            ledger,
            proposer=RandomProposer(seed=args.seed),
            templates=FUNDING_WINDOW_TEMPLATES,
            max_attempts=500,
        )
        for window in windows:
            for template in templates:
                survivors, passes = _sweep_one(
                    template,
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
        f"=== FW verdict: {total_passes}/{total_survivors} survivor(s) PASS the holdout across "
        f"{len(templates)} template(s) x {len(windows)} cell(s) ==="
    )


if __name__ == "__main__":
    main()
