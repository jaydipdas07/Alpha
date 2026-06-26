"""The cold-store ``bars_for`` adapter (B1b.3d) + the discovery-universe config.

The crown-jewel test is the **TEST-3 holdout-denial** one: after a real ``seal_dataset`` the
adapter (reading the no-ACL cold store) returns *zero* holdout-window bars — the structural
isolation the discovery loop's real backtester delegates to this boundary (R6).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.holdout import HoldoutStore, seal_dataset, split_research_holdout
from alpha_core.data.store import BarStore
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.helpers.config import DiscoveryCellConfig, DiscoveryConfig, load_discovery_config
from alpha_core.research.cold_store_bars import ColdStoreBarsFor, SeriesCoord
from alpha_core.research.engine_backtester import EngineBacktester
from alpha_core.research.strategist import StrategyProposal
from alpha_core.risk.limits import RiskConfig

SYMBOL = "NSE:RELIANCE"
DAY = timedelta(days=1)
START = datetime(2024, 1, 1, tzinfo=UTC)


def _series(
    closes: list[int],
    *,
    symbol: str = SYMBOL,
    venue: Venue = Venue.NSE,
    asset_class: AssetClass = AssetClass.EQUITY,
    interval: timedelta = DAY,
) -> list[Bar]:
    """Build a valid OHLCV series (tz-aware UTC starts, Decimal money) from a list of closes."""
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


def _equity_cell(coord: SeriesCoord | None = None) -> dict[tuple[AssetClass, str], SeriesCoord]:
    return {(AssetClass.EQUITY, "w"): coord or SeriesCoord(SYMBOL, Venue.NSE, 86400)}


# --- the TEST-3 holdout-denial proof (the reason this adapter exists) -------------------------


def test_adapter_denies_the_holdout_after_a_real_seal(tmp_path: Path) -> None:
    # a full canonical dataset; seal_dataset routes the recent 20% tail to the separate holdout
    # store and only the in-sample remainder to the cold store the adapter reads.
    full = _series([100 + i for i in range(100)])
    cold = BarStore(tmp_path / "cold")
    holdout = HoldoutStore(tmp_path / "holdout")
    window = seal_dataset(full, research=cold, holdout=holdout, fraction=0.2)
    assert window is not None
    research_expected, holdout_expected = split_research_holdout(full, window)
    assert holdout_expected, "vacuous test: the seal must actually produce a holdout"

    got = ColdStoreBarsFor(cold, _equity_cell())(AssetClass.EQUITY, "w")

    assert got, "the adapter must return the in-sample bars"
    # THE CRUX (TEST-3): not one returned bar falls in the locked holdout window.
    assert all(not window.contains(b.start) for b in got)
    assert all(b.start < window.start for b in got)
    # it is *exactly* the research partition — nothing leaked in, nothing dropped.
    assert {b.start for b in got} == {b.start for b in research_expected}
    holdout_ts = {b.start for b in holdout_expected}
    assert not (holdout_ts & {b.start for b in got})
    # belt + suspenders: the holdout bars really are sealed away in the SEPARATE store.
    sealed = holdout.read_holdout(symbol=SYMBOL, venue=Venue.NSE, interval_seconds=86400)
    assert {b.start for b in sealed} == holdout_ts


# --- resolution + pass-through -------------------------------------------------------------------


def test_reads_the_mapped_series_as_a_passthrough(tmp_path: Path) -> None:
    cold = BarStore(tmp_path / "cold")
    bars = _series([100 + i for i in range(10)])
    cold.write_bars(bars)
    got = ColdStoreBarsFor(cold, _equity_cell())(AssetClass.EQUITY, "w")
    # every stored bar, sorted by start, Decimal-exact (a pure pass-through of the cold store).
    assert [b.start for b in got] == [b.start for b in bars]
    assert all(isinstance(b.close, Decimal) for b in got)
    assert got[0].close == bars[0].close


def test_unmapped_cell_raises_a_clear_error(tmp_path: Path) -> None:
    adapter = ColdStoreBarsFor(BarStore(tmp_path / "cold"), _equity_cell())
    # a config gap fails fast + cell-identifying, never a silent empty the rigor gate misreads.
    with pytest.raises(ValueError, match="no discovery cell mapped for CRYPTO"):
        adapter(AssetClass.CRYPTO, "nope")


def test_cells_property_is_a_copy(tmp_path: Path) -> None:
    adapter = ColdStoreBarsFor(BarStore(tmp_path / "cold"), _equity_cell())
    snapshot = adapter.cells
    snapshot.clear()  # mutating the returned map must not corrupt the adapter
    assert (AssetClass.EQUITY, "w") in adapter.cells


def test_mapped_but_absent_series_returns_empty(tmp_path: Path) -> None:
    # a mapped cell whose series isn't ingested yet returns [] (EngineBacktester's >= 2*n_groups
    # bars guard surfaces the thin cell downstream) — not masked, not an error here.
    adapter = ColdStoreBarsFor(BarStore(tmp_path / "cold"), _equity_cell())
    assert adapter(AssetClass.EQUITY, "w") == []


def test_asset_class_mismatch_raises(tmp_path: Path) -> None:
    # a config row mapping a cell's market to a series of a *different* asset class is caught at
    # read — else it would feed the wrong instrument's bars through the cost model unnoticed.
    cold = BarStore(tmp_path / "cold")
    cold.write_bars(
        _series(
            [100 + i for i in range(5)],
            symbol="BTCUSDT",
            venue=Venue.BINANCE,
            asset_class=AssetClass.CRYPTO,
            interval=timedelta(minutes=5),
        )
    )
    adapter = ColdStoreBarsFor(
        cold, {(AssetClass.EQUITY, "w"): SeriesCoord("BTCUSDT", Venue.BINANCE, 300)}
    )
    with pytest.raises(ValueError, match="not EQUITY"):
        adapter(AssetClass.EQUITY, "w")


# --- from_config wires the committed discovery universe ------------------------------------------


def test_from_config_builds_the_universe_map(tmp_path: Path) -> None:
    # reads the committed config/discovery.yaml — config is the contract.
    cells = ColdStoreBarsFor.from_config(BarStore(tmp_path / "cold")).cells
    assert cells[(AssetClass.CRYPTO, "btcusdt-5m")] == SeriesCoord("BTCUSDT", Venue.BINANCE, 300)
    assert cells[(AssetClass.EQUITY, "reliance-1d")] == SeriesCoord(
        "NSE:RELIANCE", Venue.NSE, 86400
    )


def test_load_discovery_config_is_valid() -> None:
    cfg = load_discovery_config()
    assert cfg.cells, "the seeded discovery universe must be non-empty"


# --- the discovery-universe config validation ----------------------------------------------------


def test_config_rejects_empty_universe() -> None:
    # an empty discovery universe is a config error — fail at load, not at first lookup.
    with pytest.raises(ValidationError, match="at least 1"):
        DiscoveryConfig.model_validate({"cells": []})


def test_config_rejects_duplicate_cells() -> None:
    with pytest.raises(ValidationError, match="duplicate discovery cell"):
        DiscoveryConfig.model_validate(
            {
                "cells": [
                    {
                        "market": "EQUITY",
                        "window": "w",
                        "symbol": "A",
                        "venue": "NSE",
                        "interval_seconds": 86400,
                    },
                    {
                        "market": "EQUITY",
                        "window": "w",
                        "symbol": "B",
                        "venue": "NSE",
                        "interval_seconds": 86400,
                    },
                ]
            }
        )


def test_config_rejects_pipe_in_window() -> None:
    # the window doubles as the proposal-ledger cell key, where "|" separates fields.
    with pytest.raises(ValidationError, match="must not contain"):
        DiscoveryCellConfig.model_validate(
            {
                "market": "EQUITY",
                "window": "a|b",
                "symbol": "S",
                "venue": "NSE",
                "interval_seconds": 86400,
            }
        )


# --- the adapter satisfies BarsFor and drives the real engine end-to-end -------------------------


def _risk() -> RiskConfig:
    return RiskConfig.model_validate(
        {
            "base_capital": "100000",
            "limits": {
                "max_gross_exposure": "1.00",
                "max_position_per_instrument": "0.50",
                "max_concurrent_positions": 5,
                "max_order_value": "0.50",
                "max_orders_per_minute": 1000,
                "max_daily_loss_halt": "0.50",
                "max_loss_per_trade": "0.10",
                "per_segment_exposure_cap": "1.00",
            },
        }
    )


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


def test_adapter_drives_the_engine_as_a_barsfor(tmp_path: Path) -> None:
    # the seam closed: the REAL adapter (not a lambda) reads a cold store, satisfies the BarsFor
    # protocol EngineBacktester wants, and runs an actual backtest into a return series.
    cold = BarStore(tmp_path / "cold")
    cold.write_bars(_series([100 + i for i in range(25)] + [125 - i for i in range(1, 26)]))
    backtester = EngineBacktester(
        bars_for=ColdStoreBarsFor(cold, _equity_cell()),  # the cold-store adapter as the seam
        instruments={SYMBOL: InstrumentMeta(asset_class=AssetClass.EQUITY)},
        risk_config=_risk(),
        cost_config=_COST_CONFIG,
    )
    proposal = StrategyProposal(
        "ma_crossover", {"fast_period": 3, "slow_period": 8}, AssetClass.EQUITY, "w", 1, "fp"
    )
    returns = backtester.run(proposal)
    assert all(isinstance(r, float) for r in returns)
    assert any(r != 0.0 for r in returns)  # the strategy actually traded the rise-then-fall


# --- real cold-store smoke (Mac-CLI / research box; skipped where the store is absent) ----------

_REAL_COLD_ROOT = Path(__file__).resolve().parents[2] / "data_cold"


@pytest.mark.skipif(
    not _REAL_COLD_ROOT.exists(), reason="real cold store absent (CI / fresh checkout)"
)
def test_real_cold_store_cells_read_back_in_sample() -> None:
    # on a box that has ingested data (B1a.1b), every configured cell reads back real, tz-aware,
    # Decimal-exact bars of the cell's asset class.
    adapter = ColdStoreBarsFor.from_config(BarStore(_REAL_COLD_ROOT))
    read_any = False
    for (market, window), _coord in adapter.cells.items():
        bars = adapter(market, window)
        if not bars:
            continue  # a configured series may simply not be ingested on this box yet
        read_any = True
        assert all(b.asset_class == market for b in bars)
        assert all(isinstance(b.close, Decimal) for b in bars)
        assert all(b.start.tzinfo is not None for b in bars)  # tz-aware (UTC internally)
    if not read_any:
        pytest.skip("no configured series present in this cold store")
