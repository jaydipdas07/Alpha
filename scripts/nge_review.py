#!/usr/bin/env python
"""The disciplined NGE-family sweep: last-half-hour momentum ± short-gamma gating on the
NIFTY minute cell (the Baltussen form, `docs/research/intraday-edge-survey-2026-07.md` §5).

The ``f1_intraday_review`` pattern with three family-specific pieces:

- **The conditioning input** is the NGE parquet ``scripts/build_nge_series.py`` computed
  from the options RESEARCH partition (never the options holdout), loaded here, and shifted
  one session (``shift_to_next_session``) BEFORE it reaches the strategy — the look-ahead
  fence lives in the pipeline. The run log pins the series' span + row count (provenance).
- **The registered window** is the OVERLAP of the equity minute cell and the options
  research partition (default 2016-02-01 → 2024-05-24): the in-sample fold is bounded to
  it so BOTH configs (unconditioned / gated) run the same tape — an unconditioned variant
  spilling into 2024-06+ would make the pair incomparable.
- **Holdout reads default to skip** — doubly gated today: the equity 1m holdout is the
  floor-thinned ~17-session window (unpowered), AND a conditioned read needs holdout-era
  NGE, which requires a sanctioned options-holdout computation (the recorded cross-family
  coupling). ``--holdout-reads auto`` exists for the day both gates clear.

Run AFTER ``build_nge_series.py``::

    ALPHA_RESEARCH_ROOT=data_research ALPHA_HOLDOUT_ROOT=data_holdout \\
        uv run python scripts/nge_review.py

The pre-registered space is 2 configs (conditioning ∈ {none, short-gamma-only}), one cell,
exhaustive (``max_attempts=500``). Record every holdout read in TASKS.md.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from alpha_core.core.enums import AssetClass
from alpha_core.core.models import Bar
from alpha_core.data.holdout import HoldoutStore
from alpha_core.data.store import BarStore
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.helpers.config import DiscoveryCellConfig, load_discovery_config, load_yaml
from alpha_core.research.cold_store_bars import ColdStoreBarsFor
from alpha_core.research.cost_scenarios import futures_costed_equity_config
from alpha_core.research.discovery import Backtester
from alpha_core.research.engine_backtester import EngineBacktester
from alpha_core.research.holdout_gate import HoldoutBarsFor, HoldoutGate
from alpha_core.research.nge import NgeDay, shift_to_next_session
from alpha_core.research.nge_templates import nge_templates
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

_ROOT = Path(__file__).resolve().parents[1]
_CELL_WINDOW = "nifty50-1m"
# The registered fold window: the equity-cell x options-research OVERLAP (see module doc).
_WINDOW_LO = datetime(2016, 2, 1, tzinfo=UTC)
_WINDOW_HI = datetime(2024, 5, 24, tzinfo=UTC)


def _load_nge(underlying: str) -> list[NgeDay]:
    root = Path(os.environ.get("ALPHA_NGE_ROOT") or _ROOT / "data_research_nge")
    path = root / f"NSE__{underlying}__nge.parquet"
    if not path.is_file():
        raise SystemExit(f"NGE series missing: {path} — run scripts/build_nge_series.py first")
    rows = pq.read_table(path).to_pylist()
    return [
        NgeDay(day=r["day"], nge=float(r["nge"]), n_contracts=int(r["n_contracts"])) for r in rows
    ]


def _cell(cells: Sequence[DiscoveryCellConfig], window: str) -> DiscoveryCellConfig:
    for cell in cells:
        if cell.window == window:
            return cell
    raise SystemExit(f"cell {window!r} not in config/discovery.yaml")


def main() -> None:
    ap = argparse.ArgumentParser(description="Seeded NGE-family sweep (Baltussen form)")
    ap.add_argument("--seed", type=int, default=10)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--underlying", default="NIFTY")
    ap.add_argument(
        "--holdout-reads",
        choices=("auto", "skip"),
        default="skip",
        help="default SKIP (doubly gated: the equity window is floor-thinned AND a "
        "conditioned read needs a sanctioned options-holdout NGE computation — the "
        "recorded cross-family coupling); 'auto' for the day both gates clear.",
    )
    args = ap.parse_args()

    series = _load_nge(args.underlying)
    signs = shift_to_next_session(series)
    neg = sum(1 for v in signs.values() if v < 0)
    print(
        f"=== NGE input: {len(series)} raw days ({series[0].day.date()} -> "
        f"{series[-1].day.date()}), {len(signs)} shifted keys, {neg} short-gamma days "
        f"({neg / max(len(signs), 1):.1%}) ==="
    )
    templates = nge_templates(signs)

    cell = _cell(load_discovery_config().cells, _CELL_WINDOW)
    research = BarStore(Path(os.environ["ALPHA_RESEARCH_ROOT"]))
    holdout = HoldoutStore(Path(os.environ["ALPHA_HOLDOUT_ROOT"]))
    risk_config = load_risk_config().model_copy(update={"base_capital": cell.starting_cash})
    cost_config = futures_costed_equity_config(load_yaml("costs.yaml"))
    schedule = schedule_for(frozenset({AssetClass.EQUITY}))

    research_bars = ColdStoreBarsFor.from_config(research)
    holdout_bars = HoldoutBarsFor.from_config(holdout)

    def windowed(market: AssetClass, window: str) -> list[Bar]:
        # BOTH configs run the same registered tape (module docstring).
        return [b for b in research_bars(market, window) if _WINDOW_LO <= b.start < _WINDOW_HI]

    def make(bars_for: object) -> Backtester:
        return EngineBacktester(
            bars_for=bars_for,  # type: ignore[arg-type]
            instruments={cell.symbol: InstrumentMeta(asset_class=cell.market)},
            risk_config=risk_config,
            cost_config=cost_config,
            venue=cell.venue,
            starting_cash=cell.starting_cash,
            templates=templates,
            schedule=schedule,
            intraday_square_off=True,  # MIS-required family, both sides (rigor symmetry)
        )

    in_sample, holdout_bt = make(windowed), make(holdout_bars)
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
            templates=templates,
            max_attempts=500,
        )
        proposals: list[StrategyProposal] = []
        returns: list[Sequence[float]] = []
        for _ in range(args.n):
            try:
                proposal = strategist.propose(
                    "nifty_lhh_momentum", market=cell.market, window=cell.window
                )
            except CellSaturated:
                print(
                    f"  [nifty_lhh_momentum] cell saturated after {len(proposals)} "
                    "proposals (exhaustive)"
                )
                break
            proposals.append(proposal)
            returns.append(list(in_sample.run(proposal)))
        n_trials = ledger.count(cell.market, "nifty_lhh_momentum", cell.window)
        _, variance = deflation_inputs(returns, oos_fraction=qa.oos_fraction)
        assessments = [
            (
                p,
                qa.assess(
                    series_,
                    n_trials=n_trials,
                    trial_sharpe_variance=variance,
                    oos_fraction=qa.oos_fraction,
                ),
            )
            for p, series_ in zip(proposals, returns, strict=True)
        ]
        for p, a in assessments:
            print(
                f"  {dict(p.params)}: {a.verdict.name} "
                f"(DSR={a.deflated_sharpe:.3f}, OOS sharpe={a.oos_sharpe:+.4f})"
            )
        survivors = [(p, a) for p, a in assessments if a.verdict is Verdict.PROMOTE]
        print(
            f"=== nifty_lhh_momentum / {cell.market.value}/{cell.window} === "
            f"{len(proposals)} candidates, n_trials={n_trials}, {len(survivors)} survivor(s); "
            f"DSR variance={variance:.3e}"
        )
        total_survivors = len(survivors)
        if gate is None:
            for p, a in survivors:
                print(
                    f"  survivor {dict(p.params)}: in-sample OOS={a.oos_sharpe:+.4f} -> "
                    "FROZEN (holdout read deliberately skipped)"
                )
        else:
            for p, a in survivors:
                result = gate.evaluate(p, n_trials=n_trials, trial_sharpe_variance=variance)
                total_passes += result.passed
                print(
                    f"  survivor {dict(p.params)}: in-sample OOS={a.oos_sharpe:+.4f} -> "
                    f"HOLDOUT {'PASS' if result.passed else 'REJECT'} "
                    f"(verdict={result.verdict.name}, oos_sharpe={result.oos_sharpe:+.4f}, "
                    f"n_obs={result.n_obs}; {result.reason})"
                )
    print(
        f"=== NGE-family verdict: {total_passes}/{total_survivors} survivor(s) PASS the "
        f"holdout (window {_WINDOW_LO.date()} -> {_WINDOW_HI.date()}) ==="
    )


if __name__ == "__main__":
    main()
