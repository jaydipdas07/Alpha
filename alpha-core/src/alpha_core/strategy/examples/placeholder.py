"""Placeholder strategy (Phase 4) — exercises the engine loop deterministically.

Not a real edge: it buys a fixed quantity on the first bar it sees for each
symbol and then holds (emits nothing further). No RNG, no look-ahead — given the
same bars it produces the same signals every run, which is what the pipeline and
replay tests need. The real strategy slots in here later as another `Strategy`.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from alpha_core.core.enums import OrderType, Side
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Signal, Tick


class PlaceholderStrategy(Strategy):
    """Buys ``quantity`` once per symbol on its first bar, then holds."""

    def __init__(
        self, *, strategy_id: str = "placeholder", quantity: Decimal = Decimal("1")
    ) -> None:
        self._strategy_id = strategy_id
        self._quantity = quantity
        self._seen: set[str] = set()

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        if bar.symbol in self._seen:
            return []
        self._seen.add(bar.symbol)
        return [
            Signal(
                strategy_id=self._strategy_id,
                symbol=bar.symbol,
                asset_class=bar.asset_class,
                side=Side.BUY,
                quantity=self._quantity,
                order_type=OrderType.MARKET,
                created_at=bar.start + bar.interval,  # decision at bar close
                reason="placeholder: first-bar entry",
            )
        ]

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []
