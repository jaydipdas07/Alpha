"""Idle strategy — runs the full pipeline but never trades.

Emits no signals, ever. Its purpose is to exercise everything *around* the edge
— data feed, reconnection, reconcile loop, observability, scheduler — without
placing a single order. Use it for infra soak tests and live smoke checks where
you want the engine alive but zero order/fill/position state to reconcile.

Swapping to a real edge is a one-line config change (``strategy:`` in the env
file); this is the deliberate "do nothing" choice, not a missing strategy.
"""

from __future__ import annotations

from collections.abc import Sequence

from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Signal, Tick


class IdleStrategy(Strategy):
    """Consumes the feed and emits nothing — a no-trade pipeline exercise."""

    def __init__(self, *, strategy_id: str = "idle") -> None:
        self._strategy_id = strategy_id

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        return []

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []
