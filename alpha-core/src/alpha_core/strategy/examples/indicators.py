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


def _ema_series(values: Sequence[Decimal], period: int) -> list[Decimal]:
    """The EMA at each bar from index ``period-1`` onward (SMA-seeded, then recursed). Empty until
    ``period`` values exist — the building block for ``ema`` and ``macd``."""
    if period <= 0 or len(values) < period:
        return []
    k = Decimal(2) / Decimal(period + 1)
    avg = sum(values[:period], Decimal(0)) / Decimal(period)  # SMA seed
    out = [avg]
    for v in values[period:]:
        avg = (v - avg) * k + avg
        out.append(avg)
    return out


def ema(values: Sequence[Decimal], period: int) -> Decimal | None:
    """Exponential moving average (SMA-seeded). ``None`` until ``period`` values exist."""
    series = _ema_series(values, period)
    return series[-1] if series else None


def macd(
    values: Sequence[Decimal], fast: int, slow: int, signal: int
) -> tuple[Decimal, Decimal, Decimal] | None:
    """``(macd_line, signal_line, histogram)``: macd_line = EMA(fast) - EMA(slow); signal_line =
    EMA(``signal``) of the macd-line series; histogram = line - signal. ``None`` until warm (needs
    ``slow + signal`` values) or on a non-positive / ``fast >= slow`` config."""
    if not 0 < fast < slow or signal <= 0 or len(values) < slow + signal:
        return None
    fast_e = _ema_series(values, fast)
    slow_e = _ema_series(values, slow)
    offset = slow - fast  # fast_e starts ``offset`` bars earlier; align to slow_e's bars
    line_series = [fast_e[i + offset] - slow_e[i] for i in range(len(slow_e))]
    signal_e = _ema_series(line_series, signal)
    if not signal_e:
        return None
    line, sig = line_series[-1], signal_e[-1]
    return line, sig, line - sig


def atr(
    highs: Sequence[Decimal], lows: Sequence[Decimal], closes: Sequence[Decimal], period: int
) -> Decimal | None:
    """Average True Range over ``period`` bars (simple mean of true ranges). Needs ``period + 1``
    aligned bars (the prior close seeds the first true range)."""
    n = len(highs)
    if period <= 0 or n < period + 1 or len(lows) != n or len(closes) != n:
        return None
    trs = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, n)
    ]
    return sum(trs[-period:], Decimal(0)) / Decimal(period)


def donchian(
    highs: Sequence[Decimal], lows: Sequence[Decimal], period: int
) -> tuple[Decimal, Decimal] | None:
    """``(highest high, lowest low)`` over the last ``period`` bars (the Donchian channel). For a
    breakout test pass the PRIOR bars (exclude the current) so a close can break the channel."""
    if period <= 0 or len(highs) < period or len(lows) != len(highs):
        return None
    return max(highs[-period:]), min(lows[-period:])
