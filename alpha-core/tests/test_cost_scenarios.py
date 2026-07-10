"""Cost-scenario tests — "taker"/"maker" pricing for the tick folds + the engine path."""

from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from alpha_core.core.enums import AssetClass, Side
from alpha_core.execution.costs import CostModel, InstrumentMeta
from alpha_core.helpers.config import load_yaml
from alpha_core.research.cost_scenarios import (
    cost_per_side,
    equity_intraday_cost_sides,
    index_future_cost_sides,
    scenario_cost_config,
)
from alpha_core.research.funding_window_backtester import taker_cost_per_side

CRYPTO = InstrumentMeta(asset_class=AssetClass.CRYPTO)
EQUITY_META = InstrumentMeta(asset_class=AssetClass.EQUITY)

# A controlled config for exact-math assertions (mirrors costs.yaml shape).
CONFIG: dict[str, Any] = {
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
    """The identical float expression the pre-scenario helper computed — exact equality, not
    approx: this is the bit-fidelity pin that three families' folds are repriced by nothing."""
    cfg = load_yaml("costs.yaml")
    fee = float(cfg["segments"]["crypto_perp"]["trading_fee"]["pct"])
    slip = float(cfg["slippage"]["crypto_perp"]["value"]) / 10_000.0
    assert cost_per_side("taker") == fee + slip


def test_maker_is_maker_fee_only_from_real_config() -> None:
    cfg = load_yaml("costs.yaml")
    assert cost_per_side("maker") == pytest.approx(
        float(cfg["segments"]["crypto_perp"]["maker_fee"]["pct"])
    )
    assert cost_per_side("maker") < cost_per_side("taker")


def test_unknown_scenario_raises() -> None:
    with pytest.raises(ValueError, match="unknown cost scenario"):
        cost_per_side("stop_hunting")


def test_tick_plane_maker_requires_maker_fee(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A costs.yaml without maker_fee must RAISE on the tick plane too — a maker-scenario
    fold must never silently price as taker (load_yaml honors ALPHA_CONFIG_DIR per call)."""
    stripped = copy.deepcopy(CONFIG)
    del stripped["segments"]["crypto_perp"]["maker_fee"]
    (tmp_path / "costs.yaml").write_text(yaml.safe_dump(stripped))
    monkeypatch.setenv("ALPHA_CONFIG_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="maker_fee"):
        cost_per_side("maker")
    # the taker path still prices from the same stripped file (fee + slippage)
    assert cost_per_side("taker") == 0.0005 + 3 / 10_000.0


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


def test_futures_overlay_reprices_equity_at_the_futures_stack() -> None:
    from alpha_core.research.cost_scenarios import futures_costed_equity_config

    cfg = load_yaml("costs.yaml")
    out = futures_costed_equity_config(cfg)
    assert cfg["segments"]["equity_intraday"] != cfg["segments"]["index_future"]  # sanity
    assert out["segments"]["equity_intraday"] == cfg["segments"]["index_future"]
    assert out["slippage"]["equity"] == cfg["slippage"]["index_future"]
    assert (
        out["slippage"]["default_spread"]["equity"]
        == cfg["slippage"]["default_spread"]["index_future"]
    )
    # the input mapping is never touched; non-equity keys pass through
    assert (
        cfg["segments"]["equity_intraday"]["stt"]["pct"]
        != out["segments"]["equity_intraday"]["stt"]["pct"]
    )
    assert out["segments"]["crypto_perp"] == cfg["segments"]["crypto_perp"]
    # end-to-end: an EQUITY sell through the overlaid CostModel pays the FUTURES STT
    b = CostModel(out).estimate(
        side=Side.SELL, quantity=Decimal("10"), ltp=Decimal("25000"), instrument=EQUITY_META
    )
    assert b.stt == Decimal("0.0005") * Decimal("10") * b.effective_fill_price


def test_futures_overlay_requires_the_index_future_homes() -> None:
    from alpha_core.research.cost_scenarios import futures_costed_equity_config

    for strip in ("segments", "slippage"):
        cfg = copy.deepcopy(load_yaml("costs.yaml"))
        if strip == "segments":
            del cfg["segments"]["index_future"]
        else:
            del cfg["slippage"]["index_future"]
        with pytest.raises(ValueError, match="futures overlay"):
            futures_costed_equity_config(cfg)


def test_real_costs_yaml_transforms_cleanly() -> None:
    """Config-drift guard: the checked-in costs.yaml must always carry the maker_fee the
    scenario needs (it also feeds basis_cost_fraction). Asserted against the yaml itself —
    never a literal — so a legitimate one-config-value retune doesn't break the suite."""
    cfg = load_yaml("costs.yaml")
    out = scenario_cost_config(cfg, "maker")
    assert (
        out["segments"]["crypto_perp"]["trading_fee"]["pct"]
        == cfg["segments"]["crypto_perp"]["maker_fee"]["pct"]
    )


def test_equity_intraday_sides_hand_arithmetic_from_real_config() -> None:
    """The G1/G3 cash-MIS cost home, pinned by INDEPENDENT hand arithmetic against the
    real costs.yaml (the #197-review MAJOR: the fold tests are self-referential, so the
    helper needs its own pin — a mis-based GST or a dropped STT must fail HERE)."""
    cfg = load_yaml("costs.yaml")
    seg = cfg["segments"]["equity_intraday"]
    slip = float(cfg["slippage"]["equity"]["value"]) / 10_000.0
    gst_base = sum(float(seg[leg]["pct"]) for leg in seg["gst"]["on"])
    common = (
        float(seg["brokerage"]["pct"])
        + float(seg["exchange_txn"]["pct"])
        + float(seg["sebi"]["pct"])
        + float(seg["gst"]["pct"]) * gst_base
        + slip
    )
    buy, sell = equity_intraday_cost_sides()
    assert buy == pytest.approx(common + float(seg["stamp_duty"]["pct"]), abs=1e-12)
    assert sell == pytest.approx(common + float(seg["stt"]["pct"]), abs=1e-12)
    # the current checked-in values, so a silent config regression is loud too
    assert buy == pytest.approx(9.2023e-4, abs=1e-8)
    assert sell == pytest.approx(11.4023e-4, abs=1e-8)
    assert seg["gst"]["on"] == ["brokerage", "exchange_txn", "sebi"]


def test_equity_intraday_sides_raises_on_non_bps_slippage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retuned slippage type must fail loud, never silently mis-divide."""
    cfg = copy.deepcopy(load_yaml("costs.yaml"))
    cfg["slippage"]["equity"] = {"type": "ticks", "value": 2}
    path = tmp_path / "costs.yaml"
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("ALPHA_CONFIG_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="bps"):
        equity_intraday_cost_sides()


def test_index_future_sides_hand_arithmetic_from_real_config() -> None:
    """The G4 futures cost home, pinned by INDEPENDENT hand arithmetic against the real
    costs.yaml (the #199-review MINOR: the fold tests are self-referential through
    _COSTS, so the helper needs its own pin — the equity precedent)."""
    cfg = load_yaml("costs.yaml")
    seg = cfg["segments"]["index_future"]
    slip = float(cfg["slippage"]["index_future"]["value"]) / 10_000.0
    gst_base = sum(float(seg[leg]["pct"]) for leg in seg["gst"]["on"])
    common = (
        float(seg["brokerage"]["pct"])
        + float(seg["exchange_txn"]["pct"])
        + float(seg["sebi"]["pct"])
        + float(seg["gst"]["pct"]) * gst_base
        + slip
    )
    buy, sell = index_future_cost_sides()
    assert buy == pytest.approx(common + float(seg["stamp_duty"]["pct"]), abs=1e-12)
    assert sell == pytest.approx(common + float(seg["stt"]["pct"]), abs=1e-12)
    # the current checked-in values, so a silent config regression is loud too
    assert buy == pytest.approx(4.95594e-4, abs=1e-8)
    assert sell == pytest.approx(9.75594e-4, abs=1e-8)
