"""R7 strategies + indicators — triggers, conviction scores, determinism."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alpha_core.core.enums import AssetClass, Side, Venue
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Signal, Tick
from alpha_core.strategy.examples.indicators import bollinger, roc, rsi, sma, stddev
from alpha_core.strategy.examples.ma_crossover import MaCrossover, MaCrossoverConfig
from alpha_core.strategy.examples.momentum_roc import MomentumRoc, MomentumRocConfig
from alpha_core.strategy.examples.rsi_bollinger import RsiBollinger, RsiBollingerConfig
from alpha_core.strategy.examples.vwap_reversion import VwapReversion, VwapReversionConfig
from alpha_core.strategy.registry import build_strategy

T0 = datetime(2026, 1, 5, 9, 15, tzinfo=UTC)


def _bar(
    close: str, i: int, *, high: str | None = None, low: str | None = None, vol: str = "1"
) -> Bar:
    c = Decimal(close)
    return Bar(
        symbol="NSE:X",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        start=T0 + timedelta(minutes=i),
        interval=timedelta(minutes=1),
        open=c,
        high=Decimal(high) if high is not None else c,
        low=Decimal(low) if low is not None else c,
        close=c,
        volume=Decimal(vol),
    )


def _run(strategy: object, closes: list[str]) -> list[Signal]:
    out: list[Signal] = []
    for i, c in enumerate(closes):
        out.extend(strategy.on_bar(_bar(c, i)))  # type: ignore[attr-defined]
    return out


# --- indicators ----------------------------------------------------------------


def test_sma_and_none_when_short() -> None:
    vals = [Decimal(x) for x in (1, 2, 3, 4)]
    assert sma(vals, 2) == Decimal("3.5")
    assert sma(vals, 5) is None


def test_stddev_and_bollinger() -> None:
    vals = [Decimal(x) for x in (2, 4, 6)]
    sd = stddev(vals, 3)  # mean 4, variance 8/3 → ~1.633
    assert sd is not None and Decimal("1.6") < sd < Decimal("1.7")
    bands = bollinger(vals, 3, Decimal(1))
    assert bands is not None
    mid, upper, lower = bands
    assert mid == Decimal(4)
    assert upper > mid > lower
    assert bollinger(vals, 4, Decimal(1)) is None


def test_rsi_extremes() -> None:
    assert rsi([Decimal(x) for x in (1, 2, 3, 4)], 3) == Decimal(100)  # all gains
    assert rsi([Decimal(1)], 3) is None


def test_roc() -> None:
    assert roc([Decimal(x) for x in (10, 11, 12)], 2) == Decimal(20)
    assert roc([Decimal(10)], 2) is None


# --- MA crossover --------------------------------------------------------------


def test_ma_crossover_emits_scored_buy_on_cross_up() -> None:
    s = MaCrossover(MaCrossoverConfig(fast_period=2, slow_period=3))
    # flat then rising → fast SMA crosses above slow SMA.
    out = _run(s, ["10", "10", "10", "10", "14", "18"])
    buys = [sig for sig in out if sig.side is Side.BUY]
    assert buys, "expected a cross-up BUY"
    assert buys[0].score is not None and buys[0].score > 0
    assert buys[0].strategy_id == "ma_crossover"


def test_ma_crossover_deterministic() -> None:
    closes = ["10", "10", "10", "12", "14", "8", "6"]
    a = _run(MaCrossover(MaCrossoverConfig(fast_period=2, slow_period=3)), closes)
    b = _run(MaCrossover(MaCrossoverConfig(fast_period=2, slow_period=3)), closes)
    assert [(x.side, x.score) for x in a] == [(x.side, x.score) for x in b]


# --- RSI / Bollinger -----------------------------------------------------------


def test_rsi_bollinger_buys_oversold_below_band() -> None:
    s = RsiBollinger(
        RsiBollingerConfig(
            rsi_period=2, oversold=Decimal(30), bollinger_period=3, num_std=Decimal(1)
        )
    )
    out = _run(s, ["10", "10", "5"])  # sharp drop: RSI→0, close below lower band
    assert out and out[0].side is Side.BUY
    assert out[0].score == Decimal(1)  # (30-0)/30


# --- Momentum / ROC ------------------------------------------------------------


def test_momentum_roc_buys_on_thrust() -> None:
    s = MomentumRoc(MomentumRocConfig(period=2, threshold_pct=Decimal(2)))
    out = _run(s, ["10", "10", "12"])  # +20% ROC over 2 bars
    assert out and out[0].side is Side.BUY
    assert out[0].score == Decimal(10)  # 20 / 2


# --- VWAP reversion ------------------------------------------------------------


def test_vwap_reversion_shorts_above_then_exits() -> None:
    s = VwapReversion(VwapReversionConfig(band_bps=Decimal(50)))
    out: list[Signal] = []
    out.extend(s.on_bar(_bar("100", 0)))  # vwap = 100
    out.extend(s.on_bar(_bar("110", 1)))  # vwap = 105, close 110 stretched → SELL
    out.extend(s.on_bar(_bar("104", 2)))  # back below vwap → exit (BUY)
    sides = [sig.side for sig in out]
    assert sides == [Side.SELL, Side.BUY]
    assert out[0].score is not None and out[1].score is None  # entry scored, exit not


# --- registry / from_config ----------------------------------------------------


def test_registry_builds_all_r7_strategies() -> None:
    for name in ("ma_crossover", "rsi_bollinger", "momentum_roc", "vwap_reversion"):
        assert build_strategy(name) is not None


# --- short / exit branches -----------------------------------------------------


def test_ma_crossover_sell_on_cross_down() -> None:
    s = MaCrossover(MaCrossoverConfig(fast_period=2, slow_period=3))
    out = _run(s, ["10", "10", "10", "10", "4", "2"])  # falling → cross down
    assert any(sig.side is Side.SELL for sig in out)


def test_momentum_roc_sell_on_drop() -> None:
    s = MomentumRoc(MomentumRocConfig(period=2, threshold_pct=Decimal(2)))
    out = _run(s, ["10", "10", "8"])  # -20% ROC
    assert out and out[0].side is Side.SELL


def test_rsi_bollinger_sell_overbought_then_exit() -> None:
    s = RsiBollinger(
        RsiBollingerConfig(
            rsi_period=2, overbought=Decimal(70), bollinger_period=3, num_std=Decimal(1)
        )
    )
    out = _run(s, ["5", "5", "10", "6"])  # spike → SELL, revert to mid → exit BUY
    assert [sig.side for sig in out] == [Side.SELL, Side.BUY]
    assert out[1].score is None  # exit unscored


def test_rsi_bollinger_long_exit_at_mid() -> None:
    s = RsiBollinger(
        RsiBollingerConfig(
            rsi_period=2, oversold=Decimal(30), bollinger_period=3, num_std=Decimal(1)
        )
    )
    out = _run(s, ["10", "10", "5", "9"])  # drop → BUY, recover to mid → exit SELL
    assert [sig.side for sig in out] == [Side.BUY, Side.SELL]


def test_vwap_reversion_long_below_then_exit() -> None:
    s = VwapReversion(VwapReversionConfig(band_bps=Decimal(50)))
    out: list[Signal] = []
    out.extend(s.on_bar(_bar("100", 0)))
    out.extend(s.on_bar(_bar("90", 1)))  # stretched below VWAP → BUY
    out.extend(s.on_bar(_bar("99", 2)))  # back above VWAP → exit SELL
    assert [sig.side for sig in out] == [Side.BUY, Side.SELL]


def test_vwap_resets_each_day() -> None:
    s = VwapReversion(VwapReversionConfig(band_bps=Decimal(50)))
    s.on_bar(_bar("100", 0))
    # Next calendar day → fresh VWAP accumulator (no carry-over position).
    next_day = Bar(
        symbol="NSE:X",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        start=T0 + timedelta(days=1),
        interval=timedelta(minutes=1),
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=Decimal("1"),
    )
    assert list(s.on_bar(next_day)) == []  # first bar of the new day → vwap == close


# --- validation + on_tick ------------------------------------------------------


def test_config_validation_fails_fast() -> None:
    with pytest.raises(ValueError, match="fast_period"):
        MaCrossover(MaCrossoverConfig(fast_period=30, slow_period=10))
    with pytest.raises(ValueError, match="period"):
        MomentumRoc(MomentumRocConfig(period=0))
    with pytest.raises(ValueError, match="threshold_pct"):
        MomentumRoc(MomentumRocConfig(threshold_pct=Decimal(0)))
    with pytest.raises(ValueError, match="oversold"):
        RsiBollinger(RsiBollingerConfig(oversold=Decimal(80), overbought=Decimal(70)))
    with pytest.raises(ValueError, match="periods"):
        RsiBollinger(RsiBollingerConfig(rsi_period=0))
    with pytest.raises(ValueError, match="band_bps"):
        VwapReversion(VwapReversionConfig(band_bps=Decimal(0)))


def test_on_tick_is_noop_for_all() -> None:
    tick = Tick(
        symbol="NSE:X",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        ts=T0,
        last_price=Decimal("100"),
    )
    strategies: list[Strategy] = [
        MaCrossover(),
        RsiBollinger(),
        MomentumRoc(),
        VwapReversion(),
    ]
    for strat in strategies:
        assert list(strat.on_tick(tick)) == []


def test_roc_zero_past_returns_none() -> None:
    assert roc([Decimal(0), Decimal(1), Decimal(2)], 2) is None
