"""Strategy engine + placeholder tests (Phase 4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alpha_core.core.enums import AssetClass, OrderType, Side, Venue
from alpha_core.core.models import Bar
from alpha_core.data.feed import ReplayFeed
from alpha_core.strategy.engine import StrategyEngine
from alpha_core.strategy.examples.placeholder import PlaceholderStrategy

T0 = datetime(2026, 6, 15, 4, 0, tzinfo=UTC)
ONE_MIN = timedelta(minutes=1)


def _bar(symbol: str, n: int, close: str = "100") -> Bar:
    c = Decimal(close)
    return Bar(
        symbol=symbol,
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        start=T0 + n * ONE_MIN,
        interval=ONE_MIN,
        open=Decimal("100"),
        high=max(Decimal("100"), c) + Decimal("1"),
        low=min(Decimal("100"), c) - Decimal("1"),
        close=c,
        volume=Decimal("1000"),
    )


async def test_engine_emits_placeholder_signal() -> None:
    feed = ReplayFeed(bars=[_bar("NSE:RELIANCE", 0), _bar("NSE:RELIANCE", 1)])
    engine = StrategyEngine(PlaceholderStrategy(quantity=Decimal("5")))
    signals = [s async for s in engine.run_bars(feed, ["NSE:RELIANCE"])]
    # one signal: first bar only (then holds)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.side is Side.BUY
    assert sig.order_type is OrderType.MARKET
    assert sig.quantity == Decimal("5")
    assert sig.symbol == "NSE:RELIANCE"
    assert sig.created_at == T0 + ONE_MIN  # decision at first bar's close


async def test_engine_signal_per_symbol() -> None:
    feed = ReplayFeed(bars=[_bar("NSE:RELIANCE", 0), _bar("NSE:TCS", 0), _bar("NSE:RELIANCE", 1)])
    engine = StrategyEngine(PlaceholderStrategy())
    signals = [s async for s in engine.run_bars(feed, ["NSE:RELIANCE", "NSE:TCS"])]
    assert {s.symbol for s in signals} == {"NSE:RELIANCE", "NSE:TCS"}
    assert len(signals) == 2  # one per symbol


async def test_engine_deterministic() -> None:
    bars = [_bar("NSE:RELIANCE", n, close=str(100 + n)) for n in range(5)]
    engine = StrategyEngine(PlaceholderStrategy())
    first = [s.created_at async for s in engine.run_bars(ReplayFeed(bars=bars), ["NSE:RELIANCE"])]
    engine2 = StrategyEngine(PlaceholderStrategy())
    second = [s.created_at async for s in engine2.run_bars(ReplayFeed(bars=bars), ["NSE:RELIANCE"])]
    assert first == second


async def test_placeholder_emits_nothing_on_tick() -> None:
    strat = PlaceholderStrategy()
    from alpha_core.core.models import Tick

    tick = Tick(
        symbol="NSE:RELIANCE",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        ts=T0,
        last_price=Decimal("100"),
    )
    assert strat.on_tick(tick) == []


async def test_idle_emits_nothing() -> None:
    from alpha_core.core.models import Tick
    from alpha_core.strategy.examples.idle import IdleStrategy

    strat = IdleStrategy()
    # never trades, on any bar or tick
    feed = ReplayFeed(bars=[_bar("NSE:RELIANCE", 0), _bar("NSE:RELIANCE", 1)])
    signals = [s async for s in StrategyEngine(strat).run_bars(feed, ["NSE:RELIANCE"])]
    assert signals == []
    tick = Tick(
        symbol="NSE:RELIANCE",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        ts=T0,
        last_price=Decimal("100"),
    )
    assert strat.on_tick(tick) == []
