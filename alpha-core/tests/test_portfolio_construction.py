"""Portfolio construction tests (R4) — pure, deterministic allocation."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from alpha_core.core.enums import AssetClass, OrderType, Side
from alpha_core.core.models import Signal, TargetExposure
from alpha_core.helpers.config import PortfolioConfig
from alpha_core.portfolio.construction import WeightAllocator, diff_targets

T0 = datetime(2026, 1, 1, 9, 15, tzinfo=UTC)


_REBALANCE_KEYS = {"no_trade_band", "turnover_cap", "min_position_weight"}


def _cfg(**overrides: Any) -> PortfolioConfig:
    allocation = {
        "method": "equal_weight",
        "top_k": 5,
        "max_weight_per_name": "0.25",
        "gross_cap": "1.0",
    }
    rebalance = {"no_trade_band": "0.02", "turnover_cap": "1.0", "min_position_weight": "0.05"}
    for key, value in overrides.items():
        (rebalance if key in _REBALANCE_KEYS else allocation)[key] = value
    return PortfolioConfig.model_validate({"allocation": allocation, "rebalance": rebalance})


def _sig(symbol: str, side: Side, score: str | None, strat: str = "s1") -> Signal:
    return Signal(
        strategy_id=strat,
        symbol=symbol,
        asset_class=AssetClass.EQUITY,
        side=side,
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
        created_at=T0,
        score=None if score is None else Decimal(score),
    )


def _by_symbol(targets: list[TargetExposure]) -> dict[str, TargetExposure]:
    return {t.symbol: t for t in targets}


def test_equal_weight_top_k_ranks_by_score() -> None:
    # top_k=2, max_weight 0.5 → the two strongest get 0.5 each.
    cfg = _cfg(top_k=2, max_weight_per_name="0.5")
    sigs = [
        _sig("A", Side.BUY, "1"),
        _sig("B", Side.BUY, "3"),
        _sig("C", Side.BUY, "2"),
    ]
    out = _by_symbol(WeightAllocator(cfg).construct(sigs, [], Decimal("1000000")))
    assert set(out) == {"B", "C"}  # A (lowest score) dropped
    assert out["B"].weight == Decimal("0.5")
    assert out["C"].weight == Decimal("0.5")


def test_max_weight_caps_equal_weight() -> None:
    cfg = _cfg(top_k=2, max_weight_per_name="0.25")
    sigs = [_sig("A", Side.BUY, "2"), _sig("B", Side.BUY, "1")]
    out = _by_symbol(WeightAllocator(cfg).construct(sigs, [], Decimal("1000000")))
    assert out["A"].weight == Decimal("0.25")  # capped below 1/2


def test_sell_is_negative_weight() -> None:
    out = WeightAllocator(_cfg(top_k=1)).construct(
        [_sig("A", Side.SELL, "5")], [], Decimal("1000000")
    )
    assert out[0].weight < 0


def test_same_symbol_netted_with_attribution() -> None:
    # Two strategies, same symbol, same side → netted into one target, both credited.
    sigs = [_sig("A", Side.BUY, "2", "s1"), _sig("A", Side.BUY, "1", "s2")]
    out = WeightAllocator(_cfg(top_k=5)).construct(sigs, [], Decimal("1000000"))
    assert len(out) == 1
    assert out[0].strategy_id == "s1+s2"


def test_offsetting_signals_drop_out() -> None:
    sigs = [_sig("A", Side.BUY, "2", "s1"), _sig("A", Side.SELL, "2", "s2")]
    assert WeightAllocator(_cfg()).construct(sigs, [], Decimal("1000000")) == []


def test_gross_cap_scales_down() -> None:
    # 5 names x 0.2 = 1.0 gross > gross_cap 0.8 -> scaled down to sum 0.8.
    cfg = _cfg(top_k=5, max_weight_per_name="0.25", gross_cap="0.8")
    sigs = [_sig(s, Side.BUY, str(i)) for i, s in enumerate("ABCDE", start=1)]
    out = WeightAllocator(cfg).construct(sigs, [], Decimal("1000000"))
    assert sum(abs(t.weight) for t in out) == Decimal("0.8")


def test_unfundable_config_rejected_at_load() -> None:
    # CONFIG-1: a top_k=5 book caps each name at min(1/5, 0.15)=0.15 of capital;
    # a 0.20 fragmentation floor can never be met -> reject at load, not silently
    # trade nothing at runtime.
    with pytest.raises(ValueError, match="unfundable portfolio"):
        _cfg(top_k=5, max_weight_per_name="0.15", min_position_weight="0.20")


def test_unfundable_via_high_top_k_rejected() -> None:
    # 1/top_k below the floor is also unfundable (over-diversified book = dust).
    with pytest.raises(ValueError, match="unfundable portfolio"):
        _cfg(top_k=50, max_weight_per_name="0.5", min_position_weight="0.05")


def test_pilot_config_funds_the_full_book() -> None:
    # Regression for CONFIG-1: the shipped pilot shape (top_k=5, max_weight 0.15,
    # floor 0.05 = 5% of capital) funds all five names at pilot capital — the old
    # absolute 5000 INR floor silently dropped every name at 25000 capital.
    cfg = _cfg(top_k=5, max_weight_per_name="0.15", min_position_weight="0.05")
    sigs = [_sig(s, Side.BUY, str(i)) for i, s in enumerate("ABCDE", start=1)]
    out = WeightAllocator(cfg).construct(sigs, [], Decimal("25000"))
    assert len(out) == 5
    assert all(abs(t.weight) == Decimal("0.15") for t in out)


def test_no_signals_is_empty() -> None:
    assert WeightAllocator(_cfg()).construct([], [], Decimal("1000000")) == []


def test_none_score_treated_as_zero() -> None:
    # A scored name outranks an unscored one; both still tradeable if top_k allows.
    cfg = _cfg(top_k=1, max_weight_per_name="1.0")
    sigs = [_sig("A", Side.BUY, None), _sig("B", Side.BUY, "1")]
    out = WeightAllocator(cfg).construct(sigs, [], Decimal("1000000"))
    assert [t.symbol for t in out] == ["B"]


def test_inverse_vol_not_yet_supported() -> None:
    with pytest.raises(NotImplementedError, match="volatility source"):
        WeightAllocator(_cfg(method="inverse_vol")).construct(
            [_sig("A", Side.BUY, "1")], [], Decimal("1000000")
        )


def test_deterministic() -> None:
    cfg = _cfg(top_k=3)
    sigs = [_sig(s, Side.BUY, str(i)) for i, s in enumerate("ABCDE", start=1)]
    a = WeightAllocator(cfg).construct(sigs, [], Decimal("1000000"))
    b = WeightAllocator(cfg).construct(sigs, [], Decimal("1000000"))
    assert a == b


# --- diff_targets (the rebalance) ---------------------------------------------


def _target(symbol: str, weight: str) -> TargetExposure:
    return TargetExposure(
        strategy_id="s1",
        symbol=symbol,
        asset_class=AssetClass.EQUITY,
        weight=Decimal(weight),
    )


def test_diff_no_trade_band_ignores_small_moves() -> None:
    cfg = _cfg()  # no_trade_band 0.02
    targets = [_target("A", "0.105")]
    current = {"A": Decimal("0.10")}  # delta 0.005 < band
    assert diff_targets(targets, current, cfg) == {}


def test_diff_emits_delta_above_band() -> None:
    cfg = _cfg()
    targets = [_target("A", "0.25")]
    current = {"A": Decimal("0.10")}
    out = diff_targets(targets, current, cfg)
    assert out == {"A": Decimal("0.15")}


def test_diff_exits_dropped_names() -> None:
    cfg = _cfg()
    current = {"A": Decimal("0.20")}  # not in targets → exit to 0
    assert diff_targets([], current, cfg) == {"A": Decimal("-0.20")}


def test_diff_turnover_cap_scales() -> None:
    # Two 0.6 deltas = 1.2 turnover > cap 1.0 -> scaled to total 1.0.
    cfg = _cfg()
    targets = [_target("A", "0.6"), _target("B", "0.6")]
    out = diff_targets(targets, {}, cfg)
    assert sum(abs(d) for d in out.values()) == Decimal("1.0")
