#!/usr/bin/env python
"""The frozen-five under the POST-ONLY FILL MODEL — the read instrument for the maker-F2
survivors (2026-07-07's registration; run log ``f2_maker_sweep.log``).

NO SEARCH HAPPENS HERE. The five (template, cell, params) tuples below are the maker-F2
registration's frozen survivors, re-MEASURED (all five unconditionally — measurement,
never selection, so no new trials burn) under deployable maker execution:

- entries rest a **post-only limit at the decision close**, alive exactly through the
  next 1h bar (``ohlc_ticks``: the bar's extremes can trade through it; a TOUCH never
  fills — strict trade-through at the LIMIT price; unfilled → the window is missed);
- exits are **market reduce-only** (taker leg — guaranteed flat; a missed entry can
  never be inverted, #179's bug class);
- costs: the maker scenario config (post-only fee, zero crossing legs) — the entry leg;
  the exit's market leg under the same config is the DECLARED simplification (the
  scenario prices both legs at maker fees; Delta's real taker exit adds ~4bps/side —
  noted in the honesty ledger below, conservative direction NOT guaranteed: read the
  verdict with the exit-leg caveat).

DSR deflation uses the REGISTRATION's own trial inputs (n_trials=9 per (template, cell)
block; the block's trial-Sharpe variance from the run log) — the frozen five carry their
original multiplicity, not a flattering n=1.

``--holdout-reads`` defaults to **skip** (in-sample-under-fills first — the fill model's
damage report); ``auto`` spends the five one-shot reads on the gate-only 1h holdout
(2026-04-16 floor) under IDENTICAL execution — the read [You] asked this instrument for.

    ALPHA_RESEARCH_ROOT=data_research ALPHA_HOLDOUT_ROOT=data_holdout \\
        uv run python scripts/maker_fill_review.py [--holdout-reads auto]
"""

from __future__ import annotations

import argparse
import os
from decimal import Decimal
from pathlib import Path

from alpha_core.core.enums import AssetClass
from alpha_core.data.holdout import HoldoutStore
from alpha_core.data.store import BarStore
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.helpers.config import DiscoveryCellConfig, load_discovery_config, load_yaml
from alpha_core.research.cold_store_bars import ColdStoreBarsFor
from alpha_core.research.cost_scenarios import scenario_cost_config
from alpha_core.research.engine_backtester import EngineBacktester
from alpha_core.research.holdout_gate import HoldoutBarsFor, HoldoutGate
from alpha_core.research.quant_analyst import QuantAnalyst
from alpha_core.research.seasonal_templates import SEASONAL_TEMPLATES
from alpha_core.research.strategist import StrategyProposal, StrategyTemplate
from alpha_core.risk.limits import load_risk_config
from alpha_core.strategy.examples.seasonal_window import (
    SeasonalHourLong,
    SeasonalHourLongConfig,
    SeasonalSundayTrend,
    SeasonalSundayTrendConfig,
)

# THE FROZEN FIVE (maker-F2 registration, 2026-07-07, seed 10 — f2_maker_sweep.log),
# with each block's registration-time deflation inputs (n_trials, trial-Sharpe var).
FROZEN: list[tuple[str, str, dict[str, int], int, float]] = [
    ("seasonal_hour_long", "btcusdt-1h", {"hour_start": 21, "hold_hours": 2}, 9, 3.833e-05),
    ("seasonal_hour_long", "btcusdt-1h", {"hour_start": 20, "hold_hours": 3}, 9, 3.833e-05),
    (
        "seasonal_sunday_trend",
        "btcusdt-1h",
        {"entry_hour": 22, "trend_lookback_days": 2},
        9,
        1.390e-05,
    ),
    ("seasonal_hour_long", "ethusdt-1h", {"hour_start": 21, "hold_hours": 2}, 9, 4.076e-05),
    ("seasonal_hour_long", "ethusdt-1h", {"hour_start": 20, "hold_hours": 3}, 9, 4.076e-05),
]


def _maker_templates() -> dict[str, StrategyTemplate]:
    """The seasonal registry with builds FORCED into post-only execution mode — the
    param spaces stay the registration's (resolver parity for the proposal shape)."""

    def _hour(config: SeasonalHourLongConfig) -> SeasonalHourLong:
        return SeasonalHourLong(config.model_copy(update={"entry_execution": "post_only"}))

    def _sunday(config: SeasonalSundayTrendConfig) -> SeasonalSundayTrend:
        return SeasonalSundayTrend(config.model_copy(update={"entry_execution": "post_only"}))

    base = SEASONAL_TEMPLATES
    return {
        "seasonal_hour_long": StrategyTemplate(
            "seasonal_hour_long",
            "seasonal_hour_long",
            SeasonalHourLongConfig,
            _hour,
            base["seasonal_hour_long"].param_space,
        ),
        "seasonal_sunday_trend": StrategyTemplate(
            "seasonal_sunday_trend",
            "seasonal_sunday_trend",
            SeasonalSundayTrendConfig,
            _sunday,
            base["seasonal_sunday_trend"].param_space,
        ),
    }


def _cell(cells: list[DiscoveryCellConfig], window: str) -> DiscoveryCellConfig:
    for cell in cells:
        if cell.window == window:
            return cell
    raise SystemExit(f"cell {window!r} not in config/discovery.yaml")


def main() -> None:
    ap = argparse.ArgumentParser(description="The frozen five under the post-only fill model")
    ap.add_argument(
        "--holdout-reads",
        choices=("auto", "skip"),
        default="skip",
        help="'auto' spends the FIVE one-shot holdout reads under identical maker-fill "
        "execution (record each in TASKS.md); default 'skip' = the in-sample damage "
        "report only.",
    )
    args = ap.parse_args()

    research = BarStore(Path(os.environ["ALPHA_RESEARCH_ROOT"]))
    holdout = HoldoutStore(Path(os.environ["ALPHA_HOLDOUT_ROOT"]))
    cells = list(load_discovery_config().cells)
    risk_config = load_risk_config()
    cost_config = scenario_cost_config(load_yaml("costs.yaml"), "maker")
    templates = _maker_templates()
    qa = QuantAnalyst()
    print(
        "=== THE FROZEN FIVE under the post-only fill model (strict trade-through at the "
        "limit; miss = no trade; reduce-only exits; maker scenario costs) ==="
    )
    if args.holdout_reads == "skip":
        print("=== HOLDOUT READS: SKIPPED (in-sample damage report only) ===")

    reads = 0
    for template, window, params, n_trials, variance in FROZEN:
        cell = _cell(cells, window)

        def _make(bars_for: object, c: DiscoveryCellConfig = cell) -> EngineBacktester:
            return EngineBacktester(
                bars_for=bars_for,  # type: ignore[arg-type]
                instruments={c.symbol: InstrumentMeta(asset_class=c.market)},
                risk_config=risk_config.model_copy(update={"base_capital": c.starting_cash}),
                cost_config=cost_config,
                venue=c.venue,
                starting_cash=c.starting_cash,
                templates=templates,
                ohlc_ticks=True,  # the fill model's tape: extremes cross resting limits
            )

        proposal = StrategyProposal(
            template=template,
            params={k: Decimal(v) for k, v in params.items()},
            market=AssetClass.CRYPTO,
            window=window,
            trial_index=n_trials,
            fingerprint=f"frozen-{template}-{window}-{sorted(params.items())}",
        )
        in_sample = _make(ColdStoreBarsFor.from_config(research))
        series = list(in_sample.run(proposal))
        a = qa.assess(
            series, n_trials=n_trials, trial_sharpe_variance=variance, oos_fraction=qa.oos_fraction
        )
        print(
            f"  {template}/{window} {params}: {a.verdict.name} under fills "
            f"(DSR={a.deflated_sharpe:.3f}, OOS sharpe={a.oos_sharpe:+.4f}; "
            f"registration n_trials={n_trials})"
        )
        if args.holdout_reads == "auto":
            gate = HoldoutGate(
                backtester=_make(HoldoutBarsFor.from_config(holdout)), quant_analyst=qa
            )
            result = gate.evaluate(proposal, n_trials=n_trials, trial_sharpe_variance=variance)
            reads += 1
            print(
                f"    -> HOLDOUT {'PASS' if result.passed else 'REJECT'} "
                f"(verdict={result.verdict.name}, oos_sharpe={result.oos_sharpe:+.4f}, "
                f"n_obs={result.n_obs}; {result.reason}) [one-shot read #{reads} SPENT]"
            )
    if reads:
        print(f"=== {reads} holdout read(s) SPENT — record them in TASKS.md ===")


if __name__ == "__main__":
    main()
