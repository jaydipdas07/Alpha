"""Cost-scenario tests — "taker"/"maker" pricing for the tick folds + the engine path."""

from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from alpha_core.core.enums import AssetClass, Side
from alpha_core.execution.costs import CostModel, InstrumentMeta
from alpha_core.helpers.config import load_yaml
from alpha_core.research.cost_scenarios import cost_per_side, scenario_cost_config
from alpha_core.research.funding_window_backtester import taker_cost_per_side

CRYPTO = InstrumentMeta(asset_class=AssetClass.CRYPTO)

# A controlled config for exact-math assertions (mirrors costs.yaml shape).
CONFIG: dict[str, object] = {
    "slippage": {
        "crypto_perp": {"type": "bps", "value": 3},
        "default_spread": {"crypto_perp": 0.0003},
        "stress_multiplier": 2,
    },
    "segments": {
        "crypto_perp": {
            "trading_fee": {"pct": 0.0005, "side": "both"},
            "maker_fee": {"pct": 0.0002, "side": "both"},
        },
        "equity_intraday": {"brokerage": {"pct": 0.0003, "flat": 20, "mode": "min"}},
    },
}


# --- cost_per_side (tick-fold plane) ------------------------------------------------


def test_taker_matches_the_legacy_helper_exactly() -> None:
    assert cost_per_side("taker") == taker_cost_per_side()


def test_taker_is_fee_plus_slippage_from_real_config() -> None:
    cfg = load_yaml("costs.yaml")
    fee = float(cfg["segments"]["crypto_perp"]["trading_fee"]["pct"])
    slip = float(cfg["slippage"]["crypto_perp"]["value"]) / 10_000.0
    assert cost_per_side("taker") == pytest.approx(fee + slip)


def test_maker_is_maker_fee_only_from_real_config() -> None:
    cfg = load_yaml("costs.yaml")
    assert cost_per_side("maker") == pytest.approx(
        float(cfg["segments"]["crypto_perp"]["maker_fee"]["pct"])
    )
    assert cost_per_side("maker") < cost_per_side("taker")


def test_unknown_scenario_raises() -> None:
    with pytest.raises(ValueError, match="unknown cost scenario"):
        cost_per_side("stop_hunting")


# --- scenario_cost_config (engine plane) --------------------------------------------


def test_taker_transform_is_identity() -> None:
    assert scenario_cost_config(CONFIG, "taker") is CONFIG


def test_maker_transform_reprices_without_mutating_the_input() -> None:
    before = copy.deepcopy(CONFIG)
    out = scenario_cost_config(CONFIG, "maker")
    assert before == CONFIG  # the input mapping is never touched
    seg = out["segments"]["crypto_perp"]
    assert seg["trading_fee"]["pct"] == seg["maker_fee"]["pct"] == 0.0002
    assert out["slippage"]["crypto_perp"]["value"] == 0
    assert out["slippage"]["default_spread"]["crypto_perp"] == 0
    # non-crypto segments pass through untouched
    assert out["segments"]["equity_intraday"] == CONFIG["segments"]["equity_intraday"]


def test_maker_transform_requires_maker_fee() -> None:
    cfg = copy.deepcopy(CONFIG)
    del cfg["segments"]["crypto_perp"]["maker_fee"]
    with pytest.raises(ValueError, match="maker_fee"):
        scenario_cost_config(cfg, "maker")


def test_unknown_scenario_raises_on_the_engine_plane_too() -> None:
    with pytest.raises(ValueError, match="unknown cost scenario"):
        scenario_cost_config(CONFIG, "iceberg")


def test_cost_model_prices_a_maker_order_with_no_crossing_legs() -> None:
    """End-to-end through the real CostModel: LTP-only crypto order under the maker
    scenario fills AT the print (no synthetic spread crossed, no slippage buffer) and
    pays exactly the maker fee — while the taker config charges all three legs."""
    ltp, qty = Decimal("100"), Decimal("2")
    taker = CostModel(CONFIG).estimate(side=Side.BUY, quantity=qty, ltp=ltp, instrument=CRYPTO)
    maker = CostModel(scenario_cost_config(CONFIG, "maker")).estimate(
        side=Side.BUY, quantity=qty, ltp=ltp, instrument=CRYPTO
    )
    assert maker.spread_cost == 0
    assert maker.slippage_cost == 0
    assert maker.effective_fill_price == ltp
    assert maker.brokerage == ltp * qty * Decimal("0.0002")
    assert maker.total < taker.total


def test_real_costs_yaml_transforms_cleanly() -> None:
    """Config-drift guard: the checked-in costs.yaml must always carry the maker_fee the
    scenario needs (it also feeds basis_cost_fraction)."""
    out = scenario_cost_config(load_yaml("costs.yaml"), "maker")
    assert out["segments"]["crypto_perp"]["trading_fee"]["pct"] == 0.0002
