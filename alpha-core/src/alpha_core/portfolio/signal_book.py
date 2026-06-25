"""Signal book — turn an event signal stream into current desired exposure (R5).

Strategies emit signals on *change* (enter / flip / exit), but the portfolio
allocator (R4) needs the *current* desired cross-section every bar. The book is
the bridge, and it is the **same** component in backtest and live (so they agree,
ADR 0001):

- a **scored** signal expresses a directional intent → it sets/replaces the
  ``(strategy_id, symbol)`` entry (a flip just overwrites with the new side);
- a **score=None** signal is an exit (a close, not a fresh bet) → it clears that
  entry.

``active()`` returns the standing intents, one per ``(strategy_id, symbol)``, for
the allocator to net + rank. Pure and deterministic: same signal stream → same book.
"""

from __future__ import annotations

from collections.abc import Iterable

from alpha_core.core.models import Signal


class SignalBook:
    """Standing directional intents keyed by ``(strategy_id, symbol)``."""

    def __init__(self) -> None:
        self._intents: dict[tuple[str, str], Signal] = {}

    def update(self, signals: Iterable[Signal]) -> None:
        """Apply a batch of fresh signals: scored → set intent; None → clear."""
        for sig in signals:
            key = (sig.strategy_id, sig.symbol)
            if sig.score is None:
                self._intents.pop(key, None)
            else:
                self._intents[key] = sig

    def active(self) -> list[Signal]:
        """The current standing intents (deterministic order)."""
        return [self._intents[k] for k in sorted(self._intents)]

    def __len__(self) -> int:
        return len(self._intents)
