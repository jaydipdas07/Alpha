"""Pure technical indicators for the example strategies.

Each is a pure function of a price window — no state, no I/O — so strategies stay
deterministic and look-ahead-clean: callers pass only the values up to and including
the current bar, never the future. Returns ``None`` until there is enough history.
The fuller indicator set (rsi, bollinger, roc, …) arrives with the Vega lift (B0.9).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal


def sma(values: Sequence[Decimal], period: int) -> Decimal | None:
    """Simple moving average of the last ``period`` values; ``None`` if too few."""
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:], Decimal(0)) / Decimal(period)
