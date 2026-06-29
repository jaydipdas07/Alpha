"""Richer strategy templates (M3.0) + their indicators — warmup, triggers, config validation."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alpha_core.core.enums import AssetClass, Side, Venue
from alpha_core.core.models import Bar, Signal
from alpha_core.strategy.examples.bollinger_squeeze import BollingerSqueeze, BollingerSqueezeConfig
from alpha_core.strategy.examples.donchian_atr import DonchianAtr, DonchianAtrConfig
from alpha_core.strategy.examples.indicators import atr, donchian, ema, macd
from alpha_core.strategy.examples.macd import Macd, MacdConfig
from alpha_core.strategy.examples.trend_pullback import TrendPullback, TrendPullbackConfig
from alpha_core.strategy.registry import build_strategy, known_strategies

T0 = datetime(2026, 1, 5, 9, 15, tzinfo=UTC)


def _bar(close: float, i: int, *, high: float | None = None, low: float | None = None) -> Bar:
    c = Decimal(str(close))
    return Bar(
        symbol="X",
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        start=T0 + timedelta(minutes=i),
        interval=timedelta(minutes=1),
        open=c,
        high=Decimal(str(high)) if high is not None else c,
        low=Decimal(str(low)) if low is not None else c,
        close=c,
        volume=Decimal("100"),
    )


def _sine_bars(n: int = 200) -> list[Bar]:
    """Trend + oscillation with a real high/low range (so ATR is non-trivial)."""
    out = []
    for i in range(n):
        base = 100 + 20 * math.sin(i / 15.0) + i * 0.1
        out.append(_bar(base, i, high=base + 0.5, low=base - 0.5))
    return out


def _run(
    strategy: Macd | DonchianAtr | BollingerSqueeze | TrendPullback, bars: list[Bar]
) -> list[Signal]:
    out: list[Signal] = []
    for b in bars:
        out.extend(strategy.on_bar(b))
    return out


# --- indicators ----------------------------------------------------------------


def test_ema_seeds_with_sma_then_recurses() -> None:
    vals = [Decimal(x) for x in (1, 2, 3, 4, 5)]
    assert ema(vals, 3) == Decimal("4")  # seed sma(1,2,3)=2 -> 3 -> 4 (k=0.5)
    assert ema(vals[:2], 3) is None  # warmup


def test_macd_none_until_warm_then_tuple() -> None:
    short = [Decimal(str(v)) for v in range(9)]
    assert macd(short, 3, 6, 4) is None  # needs slow+signal=10 values
    warm = [Decimal(str(100 + (i % 5))) for i in range(40)]
    result = macd(warm, 3, 6, 4)
    assert result is not None and len(result) == 3
    line, signal, hist = result
    assert hist == line - signal


def test_atr_true_range_average() -> None:
    highs = [Decimal(x) for x in (10, 12, 11)]
    lows = [Decimal(x) for x in (8, 9, 9)]
    closes = [Decimal(x) for x in (9, 11, 10)]
    # TR[1]=max(3,3,0)=3, TR[2]=max(2,0,2)=2 -> ATR(2)=2.5
    assert atr(highs, lows, closes, 2) == Decimal("2.5")
    assert atr(highs, lows, closes, 5) is None  # warmup


def test_donchian_channel_and_warmup() -> None:
    highs = [Decimal(x) for x in (10, 12, 11)]
    lows = [Decimal(x) for x in (8, 9, 9)]
    assert donchian(highs, lows, 3) == (Decimal("12"), Decimal("8"))
    assert donchian(highs, lows, 4) is None


# --- strategies ----------------------------------------------------------------


def test_macd_strategy_triggers_and_validates() -> None:
    s = Macd(MacdConfig(fast_period=5, slow_period=12, signal_period=4))
    sigs = _run(s, _sine_bars())
    assert sigs and all(isinstance(x, Signal) for x in sigs)
    assert any(x.score is not None for x in sigs) and any(
        x.score is None for x in sigs
    )  # entry+exit
    assert list(s.on_tick(None)) == []  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="fast_period < slow_period"):
        Macd(MacdConfig(fast_period=20, slow_period=10))


def test_donchian_atr_triggers_with_volatility() -> None:
    s = DonchianAtr(DonchianAtrConfig(channel_period=10, atr_period=7, atr_floor_bps=Decimal("10")))
    sigs = _run(s, _sine_bars())
    assert sigs  # the 1.5-wide bars give ATR well above a 10bps floor; the trend breaks the channel
    with pytest.raises(ValueError, match="must be positive"):
        DonchianAtr(DonchianAtrConfig(channel_period=0))


def test_bollinger_squeeze_breakout_then_exit() -> None:
    s = BollingerSqueeze(
        BollingerSqueezeConfig(period=20, num_std=Decimal("2"), squeeze_bps=Decimal("150"))
    )
    bars = [_bar(100, i) for i in range(25)] + [
        _bar(c, 25 + j) for j, c in enumerate((101, 102, 100.5, 99.5))
    ]
    sigs = _run(s, bars)
    assert sigs[0].side is Side.BUY and sigs[0].score is not None  # squeeze breakout up
    assert any(x.score is None for x in sigs)  # an exit (no score)
    with pytest.raises(ValueError, match="squeeze_bps must be positive"):
        BollingerSqueeze(BollingerSqueezeConfig(squeeze_bps=Decimal("0")))


def test_trend_pullback_buys_the_dip() -> None:
    s = TrendPullback(
        TrendPullbackConfig(trend_period=20, rsi_period=7, pullback_level=Decimal("40"))
    )
    # a strong uptrend, then a multi-bar dip (RSI < 40 while close stays above the trend SMA)
    up = [_bar(100 + i, i) for i in range(60)]
    dip = [_bar(158 - j * 0.5, 60 + j) for j in range(8)]
    sigs = _run(s, up + dip)
    assert any(x.side is Side.BUY and x.score is not None for x in sigs)
    assert any(x.score is None for x in sigs)  # the exit (RSI recovered / trend flip)
    with pytest.raises(ValueError, match="0 < pullback_level < 50"):
        TrendPullback(TrendPullbackConfig(pullback_level=Decimal("60")))


def test_bollinger_squeeze_short_side_and_no_squeeze() -> None:
    s = BollingerSqueeze(
        BollingerSqueezeConfig(period=20, num_std=Decimal("2"), squeeze_bps=Decimal("150"))
    )
    # squeeze (flat), then break BELOW the lower band -> short; then revert up to the mid -> exit
    bars = [_bar(100, i) for i in range(25)] + [
        _bar(c, 25 + j) for j, c in enumerate((99, 98, 99.5, 100.5))
    ]
    sigs = _run(s, bars)
    assert sigs[0].side is Side.SELL and sigs[0].score is not None
    assert any(x.score is None for x in sigs)  # the revert-to-mid exit
    assert list(s.on_tick(None)) == []  # type: ignore[arg-type]
    # a volatile (never-squeezed) path emits nothing
    assert (
        _run(BollingerSqueeze(BollingerSqueezeConfig(squeeze_bps=Decimal("1"))), _sine_bars()) == []
    )


def test_trend_pullback_short_side() -> None:
    s = TrendPullback(
        TrendPullbackConfig(trend_period=20, rsi_period=7, pullback_level=Decimal("40"))
    )
    # a strong downtrend, then a multi-bar rally (RSI > 60 while close stays below the trend SMA)
    down = [_bar(160 - i, i) for i in range(60)]
    rally = [_bar(102 + j * 0.5, 60 + j) for j in range(8)]
    sigs = _run(s, down + rally)
    assert any(x.side is Side.SELL and x.score is not None for x in sigs)
    assert list(s.on_tick(None)) == []  # type: ignore[arg-type]


def test_donchian_atr_exit_and_on_tick() -> None:
    s = DonchianAtr(DonchianAtrConfig(channel_period=10, atr_period=7, atr_floor_bps=Decimal("10")))
    # a steep rise that breaks the channel (enter long), then a steep drop below it (exit long)
    rise = [_bar(100 + 2 * i, i, high=100 + 2 * i + 0.1, low=100 + 2 * i - 0.1) for i in range(15)]
    drop = [
        _bar(128 - 4 * j, 15 + j, high=128 - 4 * j + 0.1, low=128 - 4 * j - 0.1) for j in range(12)
    ]
    sigs = _run(s, rise + drop)
    assert any(x.side is Side.BUY and x.score is not None for x in sigs)  # the breakout entry
    assert any(x.side is Side.SELL and x.score is None for x in sigs)  # the exit-long signal
    assert list(s.on_tick(None)) == []  # type: ignore[arg-type]


def test_richer_strategies_registered() -> None:
    for name in ("macd", "donchian_atr", "bollinger_squeeze", "trend_pullback"):
        assert name in known_strategies()
        assert build_strategy(name) is not None
