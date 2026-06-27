"""The nightly discovery run (B1b.4a) — sweep the configured universe and collect survivors.

Covers the pure loop (a cycle per (cell, template), quarantine, fail-fast on a bad template name),
per-cell engine-backtester factory (capital aligned to the cell), and the production from-config
entry point end-to-end through the real engine.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.store import BarStore
from alpha_core.helpers.config import DiscoveryCellConfig
from alpha_core.research.cold_store_bars import ColdStoreBarsFor, SeriesCoord
from alpha_core.research.discovery import DiscoveryReport
from alpha_core.research.nightly import (
    ALL_TEMPLATES,
    NightlyReport,
    QuarantinedCell,
    engine_backtester_for,
    run_nightly_discovery,
    run_nightly_discovery_from_config,
)
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.quant_analyst import QuantAnalyst
from alpha_core.research.strategist import RandomProposer, Strategist, StrategyProposal
from alpha_core.risk.limits import RiskConfig

SYMBOL = "NSE:RELIANCE"
DAY = timedelta(days=1)
START = datetime(2024, 1, 1, tzinfo=UTC)

_COST_CONFIG: dict[str, object] = {
    "slippage": {
        "equity": {"type": "bps", "value": 5},
        "crypto": {"type": "bps", "value": 8},
        "index_option": {"type": "ticks", "value": 2},
        "default_spread": {"equity": 0.0005, "crypto": 0.0008, "index_option_ticks": 1},
        "stress_multiplier": 2,
    },
    "segments": {
        "equity_intraday": {"brokerage": {"pct": 0.0003, "flat": 20, "mode": "min"}},
        "crypto": {"trading_fee": {"pct": 0.001, "side": "both"}},
    },
}


def _series(
    closes: list[int],
    *,
    symbol: str = SYMBOL,
    venue: Venue = Venue.NSE,
    asset_class: AssetClass = AssetClass.EQUITY,
    interval: timedelta = DAY,
) -> list[Bar]:
    out: list[Bar] = []
    for i, c in enumerate(closes):
        close = Decimal(c)
        out.append(
            Bar(
                symbol=symbol,
                venue=venue,
                asset_class=asset_class,
                start=START + i * interval,
                interval=interval,
                open=close - Decimal("1"),
                high=close + Decimal("2"),
                low=close - Decimal("2"),
                close=close,
                volume=Decimal("1000"),
            )
        )
    return out


def _rise_then_fall(base: int = 100, step: int = 1) -> list[int]:
    # a trend up then down so a moving-average crossover actually enters and exits.
    return [base + i * step for i in range(25)] + [
        base + 25 * step - i * step for i in range(1, 26)
    ]


def _cell(
    market: AssetClass,
    window: str,
    *,
    symbol: str = SYMBOL,
    venue: Venue = Venue.NSE,
    interval_seconds: int = 86400,
    templates: list[str] | None = None,
    starting_cash: str = "1000000",
) -> DiscoveryCellConfig:
    return DiscoveryCellConfig(
        market=market,
        window=window,
        symbol=symbol,
        venue=venue,
        interval_seconds=interval_seconds,
        templates=templates,
        starting_cash=Decimal(starting_cash),
    )


def _strategist(seed: int = 1) -> Strategist:
    return Strategist(ProposalLedger(), proposer=RandomProposer(seed=seed))


class _FakeBacktester:
    """Drives the loop without the real engine: returns a fixed series, or raises for a market."""

    def __init__(
        self, *, returns: Sequence[float] | None = None, raise_for: AssetClass | None = None
    ) -> None:
        # a low-amplitude noise series (no real edge) — enough for the quant-analyst to run.
        self._returns = (
            list(returns) if returns is not None else [0.001 * (i % 5 - 2) for i in range(80)]
        )
        self._raise_for = raise_for

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        if self._raise_for is not None and proposal.market is self._raise_for:
            raise ValueError(f"boom for {proposal.market.value}")
        return self._returns


# --- the NightlyReport aggregation --------------------------------------------------------------


def test_nightly_report_survivors_flattens_across_cells() -> None:
    p1 = StrategyProposal(
        "ma_crossover", {"fast_period": 3, "slow_period": 8}, AssetClass.EQUITY, "a", 1, "f1"
    )
    p2 = StrategyProposal(
        "momentum_roc",
        {"period": 5, "threshold_pct": Decimal("1")},
        AssetClass.CRYPTO,
        "b",
        1,
        "f2",
    )
    r1 = DiscoveryReport("ma_crossover", AssetClass.EQUITY, "a", [], [p1])
    r2 = DiscoveryReport("momentum_roc", AssetClass.CRYPTO, "b", [], [p2])
    report = NightlyReport(
        [r1, r2], [QuarantinedCell(AssetClass.EQUITY, "a", "vwap_reversion", "boom")]
    )
    assert report.survivors == [p1, p2]


def test_summary_is_json_safe_and_aligned_to_discovery_runs() -> None:
    # a real loop report -> a JSON-serializable run record shaped to the pod discovery_runs table.
    report = run_nightly_discovery(
        [_cell(AssetClass.EQUITY, "w", templates=["ma_crossover"])],
        strategist=_strategist(),
        quant_analyst=QuantAnalyst(),
        backtester_for=lambda _cell: _FakeBacktester(),
        n_candidates=3,
    )
    summary = report.summary(generated_at=datetime(2026, 6, 27, 2, 0, tzinfo=UTC))
    blob = json.loads(json.dumps(summary))  # raises if not JSON-safe (Decimal/datetime)
    assert blob["generated_at"] == "2026-06-27T02:00:00+00:00"
    assert blob["cycles"] == 1
    assert blob["quarantined_count"] == 0
    cycle = blob["reports"][0]
    assert (cycle["market"], cycle["family"], cycle["window"]) == ("EQUITY", "ma_crossover", "w")
    assert cycle["trial_count"] >= 1  # candidates assessed this run


def test_summary_serializes_survivor_decimal_params_and_quarantined() -> None:
    # the promoted + quarantined branches: Decimal params are str-encoded (exact through JSON).
    promoted = StrategyProposal(
        "rsi_bollinger",
        {"rsi_period": 14, "num_std": Decimal("2.5")},
        AssetClass.CRYPTO,
        "w",
        3,
        "fp",
    )
    report = NightlyReport(
        [DiscoveryReport("rsi_bollinger", AssetClass.CRYPTO, "w", [], [promoted])],
        [QuarantinedCell(AssetClass.EQUITY, "x", "ma_crossover", "boom")],
    )
    blob = json.loads(json.dumps(report.summary(generated_at=datetime(2026, 6, 27, tzinfo=UTC))))
    assert blob["survivor_count"] == 1
    assert blob["reports"][0]["promoted"][0]["params"] == {"rsi_period": "14", "num_std": "2.5"}
    assert blob["reports"][0]["promoted"][0]["fingerprint"] == "fp"
    assert blob["quarantined"][0] == {
        "market": "EQUITY",
        "window": "x",
        "family": "ma_crossover",
        "error": "boom",
    }


# --- the pure loop: a cycle per (cell, template) ------------------------------------------------


def test_runs_a_cycle_per_cell_and_template() -> None:
    cells = [
        _cell(AssetClass.EQUITY, "w1", templates=["ma_crossover"]),
        _cell(
            AssetClass.CRYPTO,
            "w2",
            symbol="BTCUSDT",
            venue=Venue.BINANCE,
            interval_seconds=300,
            templates=["ma_crossover", "momentum_roc"],
        ),
    ]
    report = run_nightly_discovery(
        cells,
        strategist=_strategist(),
        quant_analyst=QuantAnalyst(),
        backtester_for=lambda _cell: _FakeBacktester(),
        n_candidates=3,
    )
    # one report per (cell, template): 1 + 2 = 3; nothing quarantined.
    assert len(report.reports) == 3
    assert not report.quarantined
    assert {(r.market, r.template) for r in report.reports} == {
        (AssetClass.EQUITY, "ma_crossover"),
        (AssetClass.CRYPTO, "ma_crossover"),
        (AssetClass.CRYPTO, "momentum_roc"),
    }


def test_a_cell_with_no_templates_searches_all_registered() -> None:
    report = run_nightly_discovery(
        [_cell(AssetClass.EQUITY, "w", templates=None)],
        strategist=_strategist(),
        quant_analyst=QuantAnalyst(),
        backtester_for=lambda _cell: _FakeBacktester(),
        n_candidates=3,
    )
    assert len(report.reports) == len(ALL_TEMPLATES)
    assert {r.template for r in report.reports} == set(ALL_TEMPLATES)


def test_a_poison_combo_is_quarantined_not_fatal() -> None:
    # the crypto cell's backtester raises; the equity cell still runs (one bad combo never stalls
    # the night) — the failure is recorded with its error, not swallowed.
    cells = [
        _cell(
            AssetClass.CRYPTO,
            "wc",
            symbol="BTCUSDT",
            venue=Venue.BINANCE,
            interval_seconds=300,
            templates=["ma_crossover"],
        ),
        _cell(AssetClass.EQUITY, "we", templates=["ma_crossover"]),
    ]
    report = run_nightly_discovery(
        cells,
        strategist=_strategist(),
        quant_analyst=QuantAnalyst(),
        backtester_for=lambda _cell: _FakeBacktester(raise_for=AssetClass.CRYPTO),
        n_candidates=3,
    )
    assert [r.market for r in report.reports] == [AssetClass.EQUITY]
    assert len(report.quarantined) == 1
    q = report.quarantined[0]
    assert (q.market, q.window, q.template) == (AssetClass.CRYPTO, "wc", "ma_crossover")
    assert "boom" in q.error


def test_an_unknown_template_fails_fast_before_any_backtest() -> None:
    # a config bug (a template the registry doesn't hold) raises up front, not per-cell quarantine.
    with pytest.raises(ValueError, match="unknown template"):
        run_nightly_discovery(
            [_cell(AssetClass.EQUITY, "w", templates=["no_such_template"])],
            strategist=_strategist(),
            quant_analyst=QuantAnalyst(),
            backtester_for=lambda _cell: _FakeBacktester(),
            n_candidates=3,
        )


def test_a_cell_with_no_data_is_skipped_before_proposing() -> None:
    # cell_ready rejects the equity cell -> it is skipped WITHOUT proposing (its DSR trial count
    # stays 0, so an un-ingested cell can't over-deflate a real edge later); the ready cell runs.
    ledger = ProposalLedger()
    strategist = Strategist(ledger, proposer=RandomProposer(seed=1))
    cells = [
        _cell(AssetClass.EQUITY, "empty", templates=["ma_crossover"]),
        _cell(
            AssetClass.CRYPTO,
            "ok",
            symbol="BTCUSDT",
            venue=Venue.BINANCE,
            interval_seconds=300,
            templates=["ma_crossover"],
        ),
    ]
    report = run_nightly_discovery(
        cells,
        strategist=strategist,
        quant_analyst=QuantAnalyst(),
        backtester_for=lambda _cell: _FakeBacktester(),
        n_candidates=3,
        cell_ready=lambda cell: cell.market is AssetClass.CRYPTO,
    )
    assert [r.market for r in report.reports] == [AssetClass.CRYPTO]  # only the ready cell ran
    assert len(report.quarantined) == 1
    skipped = report.quarantined[0]
    assert (skipped.market, skipped.template) == (AssetClass.EQUITY, "*")
    assert "skipped" in skipped.error
    assert ledger.count(AssetClass.EQUITY, "ma_crossover", "empty") == 0  # never proposed
    assert ledger.count(AssetClass.CRYPTO, "ma_crossover", "ok") >= 1  # the ready cell proposed


def test_config_rejects_an_empty_templates_list() -> None:
    # omit templates (= all registered) or give a non-empty list; [] is ambiguous, not "all".
    with pytest.raises(ValidationError, match="non-empty list"):
        _cell(AssetClass.EQUITY, "w", templates=[])


# --- the per-cell engine-backtester factory + capital alignment ---------------------------------


def test_engine_backtester_for_aligns_capital_so_a_large_order_trades() -> None:
    # high-priced bars (~100k notional at quantity 1): a base_capital of 100k would block the order
    # (max_order_value 0.5 -> 50k cap), but the factory aligns base_capital to the cell's 1M
    # starting_cash, so it clears and trades. A non-flat series proves the alignment worked.
    cold = _bar_store_with(_series(_rise_then_fall(base=100_000, step=500)))
    bars_for = ColdStoreBarsFor(
        cold, {(AssetClass.EQUITY, "w"): SeriesCoord(SYMBOL, Venue.NSE, 86400)}
    )
    make = engine_backtester_for(bars_for, risk_config=_small_risk(), cost_config=_COST_CONFIG)
    backtester = make(_cell(AssetClass.EQUITY, "w", starting_cash="1000000"))
    proposal = StrategyProposal(
        "ma_crossover", {"fast_period": 3, "slow_period": 8}, AssetClass.EQUITY, "w", 1, "fp"
    )
    returns = backtester.run(proposal)
    assert any(r != 0.0 for r in returns)  # it traded -> the aligned (1M) capital cleared the order


# --- the production from-config entry point (end to end through the real engine) ----------------


def test_run_nightly_discovery_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_config = Path(__file__).resolve().parents[2] / "config"
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    for name in ("risk.yaml", "costs.yaml", "rigor.yaml"):  # the configs this path loads
        shutil.copy(real_config / name, cfg_dir / name)
    (cfg_dir / "discovery.yaml").write_text(
        "n_candidates: 4\n"
        "cells:\n"
        "  - market: EQUITY\n"
        '    window: "reliance-test"\n'
        '    symbol: "NSE:RELIANCE"\n'
        "    venue: NSE\n"
        "    interval_seconds: 86400\n"
        '    starting_cash: "1000000"\n'
        '    templates: ["ma_crossover"]\n'
    )
    monkeypatch.setenv("ALPHA_CONFIG_DIR", str(cfg_dir))
    cold = _bar_store_with(_series(_rise_then_fall()), root=tmp_path / "cold")

    report = run_nightly_discovery_from_config(
        cold, ledger_path=tmp_path / "ledger.sqlite", proposer=RandomProposer(seed=1)
    )

    assert len(report.reports) == 1  # 1 cell, 1 template
    assert not report.quarantined
    # the planted edge promotes through the REAL engine on the cold-store bars (seed=1 is proven).
    assert len(report.survivors) >= 1


# --- helpers ------------------------------------------------------------------------------------


def _bar_store_with(bars: list[Bar], *, root: Path | None = None) -> BarStore:
    import tempfile

    store = BarStore(root if root is not None else Path(tempfile.mkdtemp()) / "cold")
    store.write_bars(bars)
    return store


def _small_risk() -> RiskConfig:
    # base_capital 100k: a ~100k order would be blocked (max_order_value 0.5 -> 50k) unless the
    # nightly factory aligns base_capital up to the cell's starting_cash.
    return RiskConfig.model_validate(
        {
            "base_capital": "100000",
            "limits": {
                "max_gross_exposure": "1.00",
                "max_position_per_instrument": "1.00",
                "max_concurrent_positions": 5,
                "max_order_value": "0.50",
                "max_orders_per_minute": 1000,
                "max_daily_loss_halt": "0.90",
                "max_loss_per_trade": "0.90",
                "per_segment_exposure_cap": "1.00",
            },
        }
    )
