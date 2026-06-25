"""Rebalance — turn target weights into concrete orders (R5).

Pure and price-aware: given the allocator's `TargetExposure`s, the current book,
and last prices, it computes the per-name order to move current→target. It applies
the no-trade band + turnover cap (``diff_targets``, in weight space) and then sizes
each surviving weight delta into a lot-rounded quantity. Shared by the backtester
and the live rebalance loop (F4), so they execute identically (ADR 0001).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from alpha_core.core.enums import Side
from alpha_core.core.models import TargetExposure
from alpha_core.helpers.config import PortfolioConfig
from alpha_core.portfolio.construction import SymbolKey, diff_targets


@dataclass(frozen=True, slots=True)
class RebalanceOrder:
    """One order the rebalance wants placed (venue-agnostic, symbol-keyed)."""

    symbol: str
    side: Side
    quantity: Decimal


def _floor_to_integer(_symbol: str, quantity: Decimal) -> Decimal:
    """Default lot rounding: whole units (NSE cash = 1-share lots)."""
    return quantity.to_integral_value(rounding=ROUND_DOWN)


def rebalance_orders(
    targets: Sequence[TargetExposure],
    current_qty: dict[SymbolKey, Decimal],
    prices: dict[SymbolKey, Decimal],
    capital: Decimal,
    config: PortfolioConfig,
    round_qty: Callable[[str, Decimal], Decimal] = _floor_to_integer,
) -> list[RebalanceOrder]:
    """Orders to move ``current_qty`` toward ``targets`` (band/turnover applied)."""
    current_weights: dict[SymbolKey, Decimal] = {}
    for symbol, qty in current_qty.items():
        price = prices.get(symbol)
        if price is None or capital == 0:
            continue
        current_weights[symbol] = qty * price / capital

    target_symbols = {t.symbol for t in targets}
    orders: list[RebalanceOrder] = []
    for symbol, delta_weight in sorted(diff_targets(targets, current_weights, config).items()):
        price = prices.get(symbol)
        if price is None or price == 0:
            continue
        held = current_qty.get(symbol, Decimal(0))
        if symbol not in target_symbols and held != 0:
            # Full exit (name dropped from the target set): sell exactly the held
            # quantity. Weight-derived sizing round-trips qty→weight→qty through
            # Decimal division and a lot floor, which can leave a sub-lot residual
            # that then sits below the no-trade band forever; exit on the position.
            quantity = abs(held)
        else:
            raw_qty = delta_weight * capital / price
            quantity = round_qty(symbol, abs(raw_qty))
        if quantity <= 0:
            continue
        side = Side.BUY if delta_weight > 0 else Side.SELL
        orders.append(RebalanceOrder(symbol=symbol, side=side, quantity=quantity))
    return orders
