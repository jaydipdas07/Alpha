"""Rebalance tests (R5) — turning target weights into concrete orders."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from alpha_core.core.enums import AssetClass, Side
from alpha_core.core.models import TargetExposure
from alpha_core.helpers.config import PortfolioConfig
from alpha_core.portfolio.rebalance import rebalance_orders


def _cfg(**rebalance: Any) -> PortfolioConfig:
    reb = {"no_trade_band": "0.02", "turnover_cap": "1.0", "min_position_weight": "0.05"}
    reb.update(rebalance)
    return PortfolioConfig.model_validate(
        {
            "allocation": {
                "method": "equal_weight",
                "top_k": 5,
                "max_weight_per_name": "0.25",
                "gross_cap": "1.0",
            },
            "rebalance": reb,
        }
    )


def _target(symbol: str, weight: str) -> TargetExposure:
    return TargetExposure(
        strategy_id="s", symbol=symbol, asset_class=AssetClass.EQUITY, weight=Decimal(weight)
    )


def test_full_exit_sells_exact_held_quantity() -> None:
    """A name dropped from the targets is exited on the *position*, not on a
    weight-derived size — so a fractional holding leaves no residual (F3). With
    the default integer-lot floor, the old weight-derived path would have sold 7
    and stranded 0.6 below the no-trade band."""
    targets = [_target("B", "0.10")]  # A is no longer a target -> full exit
    current_qty = {"A": Decimal("7.6"), "B": Decimal("0")}
    prices = {"A": Decimal("100"), "B": Decimal("100")}
    orders = rebalance_orders(targets, current_qty, prices, Decimal("10000"), _cfg())

    exit_a = next(o for o in orders if o.symbol == "A")
    assert exit_a.side is Side.SELL
    assert exit_a.quantity == Decimal("7.6")  # exact full exit, no sub-lot residual


def test_short_full_exit_buys_back_exact_quantity() -> None:
    """A dropped short is fully covered (BUY for the exact absolute holding)."""
    targets = [_target("B", "0.10")]
    current_qty = {"A": Decimal("-5"), "B": Decimal("0")}
    prices = {"A": Decimal("200"), "B": Decimal("100")}
    orders = rebalance_orders(targets, current_qty, prices, Decimal("10000"), _cfg())

    exit_a = next(o for o in orders if o.symbol == "A")
    assert exit_a.side is Side.BUY
    assert exit_a.quantity == Decimal("5")


def test_partial_reduction_stays_weight_derived() -> None:
    """A name still in the targets at a lower weight is reduced by the weight
    delta (not flattened) — only full exits switch to position sizing."""
    targets = [_target("A", "0.10")]  # held at 0.20, target 0.10 -> halve
    current_qty = {"A": Decimal("20")}
    prices = {"A": Decimal("100")}
    orders = rebalance_orders(targets, current_qty, prices, Decimal("10000"), _cfg())

    a = next(o for o in orders if o.symbol == "A")
    assert a.side is Side.SELL
    assert a.quantity == Decimal("10")  # 0.10 * 10000 / 100 = 10, not the full 20
