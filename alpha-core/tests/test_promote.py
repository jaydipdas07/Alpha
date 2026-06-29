"""Survivor-promotion composer tests (M3.0, Workflow B) — `research/promote.py`.

`review_survivor` is exercised with injected fake backtesters (the holdout/in-sample reads we
control), so these are pure units. The real-data wiring is verified end-to-end off-pod via
`scripts/risk_officer_review.py --survivor` on the box. The genuine-edge series mirrors
`test_holdout_gate.py` (the proven promoting input)."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.holdout import HoldoutStore, HoldoutWindow
from alpha_core.data.store import BarStore
from alpha_core.helpers.config import DiscoveryCellConfig, load_yaml
from alpha_core.research.calibration import edge_population
from alpha_core.research.promote import (
    _param_grid,
    build_survivor_backtesters,
    make_proposal,
    parse_params,
    pick_survivor,
    reconstruct_deflation_inputs,
    review_survivor,
)
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.quant_analyst import QuantAnalyst, Verdict, deflation_inputs
from alpha_core.research.strategist import TEMPLATES, StrategyProposal, proposal_fingerprint
from alpha_core.risk.limits import load_risk_config

CELL = DiscoveryCellConfig(
    market=AssetClass.CRYPTO,
    window="avaxusdt-1d",
    symbol="AVAXUSDT",
    venue=Venue.BINANCE,
    interval_seconds=86400,
    starting_cash=Decimal("1000000"),
)


class _Fake:
    """A `Backtester` returning a fixed series for any proposal (we control each read)."""

    def __init__(self, returns: Sequence[float]) -> None:
        self._returns = list(returns)

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        return self._returns


def _series(mean: float, n: int = 240, d: float = 0.0009) -> list[float]:
    """Alternating ±d about `mean` → population stdev d, so Sharpe = mean/d (deterministic)."""
    return [mean + (d if i % 2 else -d) for i in range(n)]


# backtest Sharpe ~1.67 / paper Sharpe ~1.22 → retention ~0.73 (the --demo floors); a genuine
# edge for the holdout PROMOTE; a negative-drift series for the holdout REJECT.
_BACKTEST = _series(0.0015)
_PAPER = _series(0.0011)
_NEG = _series(-0.0011)
_EDGE = edge_population(n_candidates=1, n_obs=240, seed=4, drift=0.3)[0]


def _survivor() -> StrategyProposal:
    return make_proposal("vwap_reversion", {"band_bps": Decimal("55")}, CELL, trial_index=19)


def test_review_survivor_rejects_on_failed_holdout() -> None:
    # band_bps=55's real story: the in-sample edge reverses on the holdout → REJECT → not surfaced.
    review = review_survivor(
        proposal=_survivor(),
        in_sample_backtester=_Fake(_BACKTEST),
        holdout_backtester=_Fake(_NEG),
        paper_returns=_PAPER,
        n_trials=19,
        trial_sharpe_variance=0.0005,
        strategy_name="AVAX-vwap-55",
        venue="binance",
    )
    assert not review.passed
    assert review.gate_status == "rejected"
    assert review.holdout.verdict is Verdict.REJECT


def test_review_survivor_passes_when_both_gates_clear() -> None:
    review = review_survivor(
        proposal=_survivor(),
        in_sample_backtester=_Fake(_BACKTEST),
        holdout_backtester=_Fake(_EDGE),
        paper_returns=_PAPER,
        n_trials=1,
        trial_sharpe_variance=0.0,
        strategy_name="AVAX-vwap-55",
        venue="binance",
    )
    assert review.passed
    assert review.gate_status == "passed"
    assert review.holdout.verdict is Verdict.PROMOTE
    assert review.paper.passed


def test_request_fields_emit_no_return_series_test3() -> None:
    review = review_survivor(
        proposal=_survivor(),
        in_sample_backtester=_Fake(_BACKTEST),
        holdout_backtester=_Fake(_EDGE),
        paper_returns=_PAPER,
        n_trials=1,
        trial_sharpe_variance=0.0,
        strategy_name="AVAX-vwap-55",
        venue="binance",
    )
    fields = review.request_fields(deployment_id="dep-1", strategy_id="strat-1")
    json.dumps(fields, allow_nan=False)  # JSON/pod-safe: no NaN/inf tokens

    def _no_sequence(obj: object) -> None:
        if isinstance(obj, dict):
            for value in obj.values():
                _no_sequence(value)
        elif isinstance(obj, (list, tuple)):  # a return series / bars must NEVER cross (TEST-3)
            raise AssertionError(f"request_fields leaked a sequence: {obj!r}")

    _no_sequence(fields)


def test_param_grid_enumerates_the_vwap_space() -> None:
    grid = list(_param_grid(TEMPLATES["vwap_reversion"].param_space))
    assert len(grid) == 19  # band_bps 10..100 step 5
    assert {"band_bps": Decimal("55")} in grid


def test_parse_params_coerces_to_spec_types() -> None:
    assert parse_params({"band_bps": "55"}, "vwap_reversion") == {"band_bps": Decimal("55")}
    with pytest.raises(ValueError, match="unknown param"):
        parse_params({"bogus": "1"}, "vwap_reversion")
    with pytest.raises(ValueError, match="not a valid"):
        parse_params({"band_bps": "abc"}, "vwap_reversion")


def _avax_bars(n: int = 60) -> list[Bar]:
    """Synthetic AVAXUSDT daily bars (price oscillates ±3 about 20 → vwap_reversion trades)."""
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    bars: list[Bar] = []
    for i in range(n):
        close = Decimal(str(round(20 + 3 * math.sin(i / 3.0), 4)))
        open_ = Decimal(str(round(20 + 3 * math.sin((i - 1) / 3.0), 4)))
        bars.append(
            Bar(
                symbol="AVAXUSDT",
                venue=Venue.BINANCE,
                asset_class=AssetClass.CRYPTO,
                start=t0 + timedelta(days=i),
                interval=timedelta(days=1),
                open=open_,
                high=max(open_, close) + Decimal("0.2"),
                low=min(open_, close) - Decimal("0.2"),
                close=close,
                volume=Decimal("1000"),
            )
        )
    return bars


def test_reconstruct_and_build_survivor_backtesters(tmp_path: Path) -> None:
    # avaxusdt-1d resolves via config/discovery.yaml; we point the stores at synthetic AVAX bars.
    research = BarStore(tmp_path / "research")
    research.write_bars(_avax_bars())
    holdout = HoldoutStore(tmp_path / "holdout")
    bars = _avax_bars()
    holdout.replace(bars, HoldoutWindow(start=bars[0].start, end=bars[-1].start, version="v1"))
    risk, cost = load_risk_config(), load_yaml("costs.yaml")

    # reconstruct: 3 tried vwap configs in the ledger → n_trials=3 + a variance over their in-sample
    with ProposalLedger(tmp_path / "ledger.sqlite") as ledger:
        for band in (Decimal("50"), Decimal("55"), Decimal("60")):
            ledger.record(
                AssetClass.CRYPTO,
                "vwap_reversion",
                "avaxusdt-1d",
                proposal_fingerprint("vwap_reversion", {"band_bps": band}),
            )
        n_trials, variance = reconstruct_deflation_inputs(
            template="vwap_reversion",
            cell=CELL,
            research_store=research,
            ledger=ledger,
            risk_config=risk,
            cost_config=cost,
        )
    assert n_trials == 3
    assert variance >= 0.0

    # build: both backtesters run a proposal and yield a per-bar return series
    in_bt, hold_bt = build_survivor_backtesters(
        cell=CELL,
        research_store=research,
        holdout_store=holdout,
        risk_config=risk,
        cost_config=cost,
    )
    assert isinstance(list(in_bt.run(_survivor())), list)
    assert isinstance(list(hold_bt.run(_survivor())), list)

    # faithfulness: the reconstructed variance == deflation_inputs over exactly the 3 tried configs
    of = QuantAnalyst().oos_fraction
    population = [
        list(in_bt.run(make_proposal("vwap_reversion", {"band_bps": b}, CELL, trial_index=3)))
        for b in (Decimal("50"), Decimal("55"), Decimal("60"))
    ]
    _, expected_variance = deflation_inputs(population, oos_fraction=of)
    assert variance == pytest.approx(expected_variance)


def test_param_grid_covers_int_and_decimal_specs() -> None:
    grid = list(_param_grid(TEMPLATES["momentum_roc"].param_space))
    assert len(grid) == 38 * 10  # period 3..40 (IntRange) x threshold_pct 0.5..5 step 0.5 (Decimal)
    assert {"period": 10, "threshold_pct": Decimal("2.5")} in grid


def _run_record(*families: tuple[str, dict[str, str]]) -> dict[str, object]:
    """A discovery run record (NightlyReport.summary shape) promoting the given families on
    avaxusdt-1d, plus a non-surviving btcusdt-1d report."""
    reports: list[dict[str, object]] = [
        {"window": "avaxusdt-1d", "family": fam, "survivors": 1, "promoted": [{"params": params}]}
        for fam, params in families
    ]
    reports.append(
        {"window": "btcusdt-1d", "family": "ma_crossover", "survivors": 0, "promoted": []}
    )
    return {"reports": reports}


def test_pick_survivor_happy_path() -> None:
    rec = _run_record(("vwap_reversion", {"band_bps": "55"}))
    assert pick_survivor(rec, window="avaxusdt-1d") == ("vwap_reversion", {"band_bps": "55"})


def test_pick_survivor_no_match_raises() -> None:
    rec = _run_record(("vwap_reversion", {"band_bps": "55"}))
    with pytest.raises(ValueError, match="no survivor"):
        pick_survivor(rec, window="ethusdt-1d")


def test_pick_survivor_ambiguous_requires_template() -> None:
    rec = _run_record(
        ("vwap_reversion", {"band_bps": "55"}),
        ("ma_crossover", {"fast_period": "5", "slow_period": "20"}),
    )
    with pytest.raises(ValueError, match="pass --template"):
        pick_survivor(rec, window="avaxusdt-1d")
    assert pick_survivor(rec, window="avaxusdt-1d", template="ma_crossover") == (
        "ma_crossover",
        {"fast_period": "5", "slow_period": "20"},
    )


def test_pick_survivor_multiple_configs_in_family_raises() -> None:
    rec: dict[str, object] = {
        "reports": [
            {
                "window": "avaxusdt-1d",
                "family": "vwap_reversion",
                "survivors": 2,
                "promoted": [{"params": {"band_bps": "55"}}, {"params": {"band_bps": "60"}}],
            }
        ]
    }
    with pytest.raises(ValueError, match="narrow the selection"):
        pick_survivor(rec, window="avaxusdt-1d")
