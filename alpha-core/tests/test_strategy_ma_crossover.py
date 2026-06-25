"""MA-crossover reference strategy: contract conformance + crossover behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alpha_core.core.enums import AssetClass, OrderType, Side, Venue
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Signal, Tick
from alpha_core.strategy.examples.indicators import sma
from alpha_core.strategy.examples.ma_crossover import MaCrossover, MaCrossoverConfig

_START = datetime(2026, 1, 1, tzinfo=UTC)
_INTERVAL = timedelta(minutes=5)


def _bar(close: Decimal, i: int) -> Bar:
    return Bar(
        symbol="BTCUSDT",
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        start=_START + i * _INTERVAL,
        interval=_INTERVAL,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=Decimal("1"),
    )


def _run(strat: MaCrossover, closes: list[Decimal]) -> list[Signal]:
    out: list[Signal] = []
    for i, c in enumerate(closes):
        out.extend(strat.on_bar(_bar(c, i)))
    return out


def test_is_a_strategy() -> None:
    assert isinstance(MaCrossover(), Strategy)


def test_on_tick_is_noop() -> None:
    tick = Tick(
        symbol="BTCUSDT",
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        ts=_START,
        last_price=Decimal("100"),
    )
    assert MaCrossover().on_tick(tick) == []


def test_config_requires_fast_lt_slow() -> None:
    with pytest.raises(ValueError, match="fast_period < slow_period"):
        MaCrossover(MaCrossoverConfig(fast_period=30, slow_period=10))


def test_default_config_values() -> None:
    # the model defaults; the canonical values live in config/strategies/ma_crossover.yaml
    cfg = MaCrossover()._cfg
    assert cfg.fast_period == 10
    assert cfg.slow_period == 30


def test_warmup_emits_nothing() -> None:
    # slow SMA not yet computable over the first two bars -> no signals
    strat = MaCrossover(MaCrossoverConfig(fast_period=2, slow_period=3))
    assert _run(strat, [Decimal("10"), Decimal("11")]) == []


def test_crossover_up_then_down() -> None:
    strat = MaCrossover(MaCrossoverConfig(fast_period=2, slow_period=3, quantity=Decimal("2")))
    closes = [Decimal(x) for x in (30, 28, 26, 24, 30, 36, 24, 18)]
    signals = _run(strat, closes)

    assert [s.side for s in signals] == [Side.BUY, Side.SELL]
    buy = signals[0]
    assert buy.strategy_id == "ma_crossover"
    assert buy.symbol == "BTCUSDT"
    assert buy.asset_class is AssetClass.CRYPTO
    assert buy.order_type is OrderType.MARKET
    assert buy.quantity == Decimal("2")
    assert buy.reason == "MA cross up"
    assert buy.score is not None
    assert buy.score > 0
    # bar-time, not wall-clock: cross-up fires on bar index 4 -> start + interval
    assert buy.created_at == _START + 5 * _INTERVAL


def test_no_duplicate_signal_same_direction() -> None:
    # a sustained uptrend after the cross emits exactly one BUY, not one per bar
    strat = MaCrossover(MaCrossoverConfig(fast_period=2, slow_period=3))
    closes = [Decimal(x) for x in (30, 28, 26, 24, 30, 36, 42, 50)]
    assert [s.side for s in _run(strat, closes)] == [Side.BUY]


def test_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # no override -> resolution walks up to the repo-root config/
    monkeypatch.delenv("ALPHA_CONFIG_DIR", raising=False)
    cfg = MaCrossover.from_config()._cfg
    assert cfg.strategy_id == "ma_crossover"
    assert cfg.fast_period == 10
    assert cfg.slow_period == 30
    assert cfg.quantity == Decimal("1")


def test_sma() -> None:
    assert sma([Decimal("1"), Decimal("2")], 3) is None  # too few
    assert sma([], 0) is None  # non-positive period
    assert sma([Decimal("2"), Decimal("4"), Decimal("6")], 2) == Decimal("5")
