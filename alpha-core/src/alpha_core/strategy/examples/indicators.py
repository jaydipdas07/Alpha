"""Pure technical indicators for the example strategies (R7).

Each is a pure function of a price (or price+volume) window — no state, no I/O — so
the strategies stay deterministic and look-ahead-clean: callers pass only the bars
up to and including the current one, never the future. Each returns ``None`` when
there is not yet enough history, so a strategy simply emits nothing until warm.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from itertools import pairwise


def sma(values: Sequence[Decimal], period: int) -> Decimal | None:
    """Simple moving average of the last ``period`` values."""
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:], Decimal(0)) / Decimal(period)


def stddev(values: Sequence[Decimal], period: int) -> Decimal | None:
    """Population standard deviation of the last ``period`` values."""
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window, Decimal(0)) / Decimal(period)
    variance = sum(((v - mean) ** 2 for v in window), Decimal(0)) / Decimal(period)
    return variance.sqrt()


def bollinger(
    values: Sequence[Decimal], period: int, num_std: Decimal
) -> tuple[Decimal, Decimal, Decimal] | None:
    """(mid, upper, lower) Bollinger bands: SMA ± ``num_std`` standard deviations."""
    mid = sma(values, period)
    sd = stddev(values, period)
    if mid is None or sd is None:
        return None
    band = sd * num_std
    return mid, mid + band, mid - band


def rsi(values: Sequence[Decimal], period: int) -> Decimal | None:
    """Wilder-style RSI over ``period`` (simple average of gains/losses). 0..100."""
    if period <= 0 or len(values) < period + 1:
        return None
    window = values[-(period + 1) :]
    gains = Decimal(0)
    losses = Decimal(0)
    for prev, cur in pairwise(window):
        change = cur - prev
        if change >= 0:
            gains += change
        else:
            losses += -change
    if losses == 0:
        return Decimal(100)
    rs = (gains / Decimal(period)) / (losses / Decimal(period))
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


def roc(values: Sequence[Decimal], period: int) -> Decimal | None:
    """Rate of change in percent over ``period`` bars: (last/past - 1) * 100."""
    if period <= 0 or len(values) < period + 1:
        return None
    past = values[-(period + 1)]
    if past == 0:
        return None
    return (values[-1] - past) / past * Decimal(100)
