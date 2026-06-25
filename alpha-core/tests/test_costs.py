"""Cost model tests (ADR 0008)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from alpha_core.core.enums import AssetClass, Side
from alpha_core.execution.costs import CostBreakdown, CostModel, CostModelError, InstrumentMeta
from alpha_core.helpers.config import load_yaml

# A controlled config for exact-math assertions (mirrors costs.yaml shape).
CONFIG = {
    "slippage": {
        "equity": {"type": "bps", "value": 5},
        "crypto": {"type": "bps", "value": 8},
        "index_option": {"type": "ticks", "value": 2},
        "default_spread": {"equity": 0.0005, "crypto": 0.0008, "index_option_ticks": 1},
        "stress_multiplier": 2,
    },
    "segments": {
        "equity_intraday": {
            "brokerage": {"pct": 0.0003, "flat": 20, "mode": "min"},
            "stt": {"pct": 0.00025, "side": "sell"},
            "exchange_txn": {"pct": 0.0000297},
            "sebi": {"pct": 0.000001},
            "gst": {"pct": 0.18, "on": ["brokerage", "exchange_txn", "sebi"]},
            "stamp_duty": {"pct": 0.00003, "side": "buy"},
        },
        "index_option": {
            "brokerage": {"flat": 20, "mode": "flat"},
            "stt": {"pct": 0.000625, "side": "sell"},
            "exchange_txn": {"pct": 0.0003503},
            "sebi": {"pct": 0.000001},
            "gst": {"pct": 0.18, "on": ["brokerage", "exchange_txn", "sebi"]},
            "stamp_duty": {"pct": 0.00003, "side": "buy"},
        },
        "crypto": {
            "trading_fee": {"pct": 0.001, "side": "both"},
            "tds": {"pct": 0.01, "side": "sell"},
        },
    },
}

EQUITY = InstrumentMeta(asset_class=AssetClass.EQUITY)
CRYPTO = InstrumentMeta(asset_class=AssetClass.CRYPTO)
OPTION = InstrumentMeta(asset_class=AssetClass.INDEX_OPTION, tick_size=Decimal("0.05"))


def _model() -> CostModel:
    return CostModel(CONFIG)


def _consistent(b: CostBreakdown) -> bool:
    return b.total == (
        b.spread_cost
        + b.slippage_cost
        + b.brokerage
        + b.stt
        + b.exchange_txn
        + b.gst
        + b.sebi
        + b.stamp_duty
        + b.tds
    )


# --- price-taker fill + slippage (exact) ---------------------------------------


def test_equity_buy_quote_spread_and_slippage() -> None:
    b = _model().estimate(
        side=Side.BUY,
        quantity=Decimal("10"),
        bid=Decimal("99"),
        ask=Decimal("101"),
        instrument=EQUITY,
    )
    assert b.effective_fill_price == Decimal("101.0505")  # 101 + 101*5bps
    assert b.spread_cost == Decimal("10")  # |101-100| * 10
    assert b.slippage_cost == Decimal("0.505")  # 0.0505 * 10
    assert _consistent(b)


def test_never_assumes_mid() -> None:
    with pytest.raises(CostModelError):
        _model().estimate(side=Side.BUY, quantity=Decimal("1"), instrument=EQUITY)


def test_ltp_only_uses_default_spread() -> None:
    b = _model().estimate(
        side=Side.BUY, quantity=Decimal("1"), ltp=Decimal("100"), instrument=EQUITY
    )
    # full spread = 100 * 0.0005 = 0.05; half = 0.025; base = 100.025
    assert b.spread_cost == Decimal("0.025")
    assert b.effective_fill_price > Decimal("100.025")  # plus slippage buffer


# --- Indian stack side behavior ------------------------------------------------


def test_equity_buy_has_stamp_no_stt() -> None:
    b = _model().estimate(
        side=Side.BUY,
        quantity=Decimal("10"),
        bid=Decimal("99"),
        ask=Decimal("101"),
        instrument=EQUITY,
    )
    assert b.stt == 0
    assert b.stamp_duty > 0
    assert b.brokerage == min(
        Decimal("0.0003") * (Decimal("10") * b.effective_fill_price), Decimal("20")
    )
    assert b.gst == Decimal("0.18") * (b.brokerage + b.exchange_txn + b.sebi)


def test_equity_sell_has_stt_no_stamp() -> None:
    b = _model().estimate(
        side=Side.SELL,
        quantity=Decimal("10"),
        bid=Decimal("99"),
        ask=Decimal("101"),
        instrument=EQUITY,
    )
    assert b.stt > 0
    assert b.stamp_duty == 0
    assert b.spread_cost == Decimal("10")  # |99-100| * 10


# --- crypto --------------------------------------------------------------------


def test_crypto_tds_sell_only() -> None:
    buy = _model().estimate(
        side=Side.BUY,
        quantity=Decimal("2"),
        bid=Decimal("100"),
        ask=Decimal("100.5"),
        instrument=CRYPTO,
    )
    sell = _model().estimate(
        side=Side.SELL,
        quantity=Decimal("2"),
        bid=Decimal("100"),
        ask=Decimal("100.5"),
        instrument=CRYPTO,
    )
    assert buy.tds == 0
    assert sell.tds > 0
    assert buy.brokerage > 0 and sell.brokerage > 0  # trading_fee both sides


# --- index option (ticks slippage) ---------------------------------------------


def test_option_tick_slippage_requires_tick_size() -> None:
    no_tick = InstrumentMeta(asset_class=AssetClass.INDEX_OPTION)
    with pytest.raises(CostModelError):
        _model().estimate(
            side=Side.BUY,
            quantity=Decimal("50"),
            bid=Decimal("100"),
            ask=Decimal("100.1"),
            instrument=no_tick,
        )


def test_option_slippage_is_ticks() -> None:
    b = _model().estimate(
        side=Side.BUY,
        quantity=Decimal("50"),
        bid=Decimal("100"),
        ask=Decimal("100.10"),
        instrument=OPTION,
    )
    # buffer = 2 ticks * 0.05 = 0.10 per unit; cost = 0.10 * 50 = 5
    assert b.slippage_cost == Decimal("5.00")


# --- 2x stress -----------------------------------------------------------------


def test_stress_doubles_slippage() -> None:
    base = _model().estimate(
        side=Side.BUY,
        quantity=Decimal("10"),
        bid=Decimal("99"),
        ask=Decimal("101"),
        instrument=EQUITY,
    )
    stressed = _model().estimate(
        side=Side.BUY,
        quantity=Decimal("10"),
        bid=Decimal("99"),
        ask=Decimal("101"),
        instrument=EQUITY,
        stress=True,
    )
    assert stressed.slippage_cost == base.slippage_cost * 2


def test_quantity_must_be_positive() -> None:
    with pytest.raises(CostModelError):
        _model().estimate(
            side=Side.BUY, quantity=Decimal("0"), ltp=Decimal("100"), instrument=EQUITY
        )


# --- real config loads ---------------------------------------------------------


def test_real_costs_yaml_works(monkeypatch: pytest.MonkeyPatch) -> None:
    # Uses the committed config/costs.yaml via the default config dir.
    monkeypatch.delenv("ALPHA_CONFIG_DIR", raising=False)
    cfg = load_yaml("costs.yaml")
    model = CostModel(cfg)
    b = model.estimate(
        side=Side.SELL,
        quantity=Decimal("10"),
        bid=Decimal("99"),
        ask=Decimal("101"),
        instrument=EQUITY,
    )
    assert b.total > 0
    assert _consistent(b)
