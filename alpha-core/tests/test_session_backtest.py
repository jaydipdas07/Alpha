"""SF4 session-aware backtest gate tests (TEST-1 for session markets).

The live Worker gates ``process_bar`` on the session rules (SCHED-1, #160): blocked
bars — pre-open, holiday, past ``no_new_entry_time`` — NEVER reach the strategy.
These tests pin the backtest runner's symmetric mirror: the same bars, the same
gate, the same square-off semantics, so an Indian-market fold cannot enter where
live would refuse (Kite historical tapes carry the 15:15+ session tail).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from alpha_core.backtest.runner import BacktestResult, run_backtest
from alpha_core.core.enums import AssetClass, OrderType, Side, Venue
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Signal, Tick
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.risk.limits import RiskConfig
from alpha_core.scheduler.clock import MarketSchedule

SYMBOL = "NSE:RELIANCE"
IST = ZoneInfo("Asia/Kolkata")
ONE_MIN = timedelta(minutes=1)

# Zero-friction cost config: fills land exactly at the bar close, so P&L pins are
# exact Decimal arithmetic (the gate's *behavior* is under test, not the cost model).
FREE_COSTS: dict[str, object] = {
    "slippage": {
        "equity": {"type": "bps", "value": 0},
        "default_spread": {"equity": 0},
        "stress_multiplier": 2,
    },
    "segments": {"equity_intraday": {}},
}


def _nse_schedule() -> MarketSchedule:
    """An NSE-shaped session built directly (no config reads): 09:15-15:30 IST,
    no-new-entry 15:15, square-off 15:20, weekends closed (default calendar)."""
    return MarketSchedule(
        tz="Asia/Kolkata",
        open_time=time(9, 15),
        close_time=time(15, 30),
        no_new_entry=time(15, 15),
        square_off=time(15, 20),
    )


class EveryBarBuyer(Strategy):
    """Buys 1 on every bar it is SHOWN — ``calls`` counts exactly the bars that
    reached the strategy, pinning the state-desync half of the gate contract."""

    def __init__(self) -> None:
        self.calls = 0

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        self.calls += 1
        return [
            Signal(
                strategy_id="every-bar-buyer",
                symbol=bar.symbol,
                asset_class=bar.asset_class,
                side=Side.BUY,
                quantity=Decimal("1"),
                order_type=OrderType.MARKET,
                created_at=bar.start + bar.interval,
                reason="test: buy every shown bar",
            )
        ]

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []


def _bars(start_ist: datetime, closes: list[str]) -> list[Bar]:
    """1m NSE bars starting at ``start_ist`` (tz-aware IST), one per close."""
    out: list[Bar] = []
    for i, c in enumerate(closes):
        close = Decimal(c)
        out.append(
            Bar(
                symbol=SYMBOL,
                venue=Venue.NSE,
                asset_class=AssetClass.EQUITY,
                start=(start_ist + i * ONE_MIN).astimezone(UTC),
                interval=ONE_MIN,
                open=close,
                high=close + Decimal("1"),
                low=close - Decimal("1"),
                close=close,
                volume=Decimal("1000"),
            )
        )
    return out


def _risk() -> RiskConfig:
    return RiskConfig.model_validate(
        {
            "base_capital": "1000000",
            "limits": {
                "max_gross_exposure": "1.00",
                "max_position_per_instrument": "0.50",
                "max_concurrent_positions": 5,
                "max_order_value": "0.50",
                "max_orders_per_minute": 1000,
                "max_daily_loss_halt": "0.50",
                "max_loss_per_trade": "0.90",
                "per_segment_exposure_cap": "1.00",
            },
        }
    )


async def _run(
    bars: list[Bar],
    strategy: Strategy,
    *,
    schedule: MarketSchedule | None = None,
    intraday_square_off: bool = False,
) -> BacktestResult:
    return await run_backtest(
        bars=bars,
        strategy=strategy,
        instruments={SYMBOL: InstrumentMeta(asset_class=AssetClass.EQUITY)},
        risk_config=_risk(),
        cost_config=FREE_COSTS,
        starting_cash=Decimal("1000000"),
        schedule=schedule,
        intraday_square_off=intraday_square_off,
    )


# 2026-06-17 is a Wednesday (a plain trading day for the default calendar).
def _wed(hh: int, mm: int) -> datetime:
    return datetime(2026, 6, 17, hh, mm, tzinfo=IST)


async def test_no_schedule_is_the_pre_sf4_baseline() -> None:
    strat = EveryBarBuyer()
    result = await _run(_bars(_wed(15, 10), ["100"] * 10), strat)  # runs into the tail
    assert strat.calls == 10  # every bar reaches the strategy without a schedule
    assert result.stats.num_fills == 11  # 10 buys + the terminal square-off flatten


async def test_no_new_entry_cutoff_gates_the_session_tail() -> None:
    # Bars 15:10→15:20 IST: closes 15:11..15:15..15:20. The live Worker blocks the
    # strategy from the 15:15:00 close onward (>= no_new_entry) — so must we.
    strat = EveryBarBuyer()
    result = await _run(_bars(_wed(15, 10), ["100"] * 10), strat, schedule=_nse_schedule())
    assert strat.calls == 4  # closes 15:11, 15:12, 15:13, 15:14 only
    assert result.stats.num_fills == 5  # 4 buys + the terminal flatten


async def test_pre_open_and_weekend_bars_never_reach_the_strategy() -> None:
    strat = EveryBarBuyer()
    # Pre-open ticks (Kite pushes 09:00-09:07 snapshot prints): closes 09:06..09:10 — all < 09:15.
    pre_open = await _run(_bars(_wed(9, 5), ["100"] * 5), strat, schedule=_nse_schedule())
    assert strat.calls == 0 and pre_open.stats.num_fills == 0
    # Saturday 2026-06-20: the default calendar closes weekends outright.
    sat = datetime(2026, 6, 20, 10, 0, tzinfo=IST)
    weekend = await _run(_bars(sat, ["100"] * 5), strat, schedule=_nse_schedule())
    assert strat.calls == 0 and weekend.stats.num_fills == 0


async def test_24x7_schedule_is_bit_identical_to_none() -> None:
    # The frozen-crypto-verdict guard: a 24x7 schedule must change NOTHING.
    bars = [
        Bar(
            symbol="BTCUSDT",
            venue=Venue.BINANCE,
            asset_class=AssetClass.CRYPTO,
            start=datetime(2026, 6, 17, 3, i, tzinfo=UTC),
            interval=ONE_MIN,
            open=Decimal(str(100 + i)),
            high=Decimal(str(101 + i)),
            low=Decimal(str(99 + i)),
            close=Decimal(str(100 + i)),
            volume=Decimal("1"),
        )
        for i in range(6)
    ]
    crypto_costs: dict[str, object] = {
        "slippage": {
            "crypto_perp": {"type": "bps", "value": 0},
            "default_spread": {"crypto_perp": 0},
            "stress_multiplier": 2,
        },
        "segments": {"crypto_perp": {}},
    }

    async def run(schedule: MarketSchedule | None) -> BacktestResult:
        return await run_backtest(
            bars=bars,
            strategy=EveryBarBuyer(),
            instruments={"BTCUSDT": InstrumentMeta(asset_class=AssetClass.CRYPTO)},
            risk_config=_risk(),
            cost_config=crypto_costs,
            venue=Venue.BINANCE,
            starting_cash=Decimal("1000000"),
            schedule=schedule,
        )

    base = await run(None)
    always_open = await run(MarketSchedule(is_24x7=True))
    assert base.equity_curve == always_open.equity_curve
    assert base.stats == always_open.stats


class OneLowballLimit(Strategy):
    """Rests ONE far-below-market limit buy on the first bar, then goes quiet — the
    review #176 probe-C shape: without the Worker's cancel-working leg, this order
    survives the square-off and fills in the blocked window when prices drop."""

    def __init__(self) -> None:
        self._done = False

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        if self._done:
            return []
        self._done = True
        return [
            Signal(
                strategy_id="lowball",
                symbol=bar.symbol,
                asset_class=bar.asset_class,
                side=Side.BUY,
                quantity=Decimal("1"),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("150"),  # far below the 200s tape — rests
                created_at=bar.start + bar.interval,
                reason="test: resting lowball",
            )
        ]

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []


async def test_square_off_cancels_resting_orders_before_the_blocked_window() -> None:
    # Tape: 200s until the 15:20 cutoff, then a post-cutoff crash through the limit.
    closes = ["200"] * 10 + ["140"] * 4  # crash bars close 15:21..15:24 (blocked window)
    bars = _bars(_wed(15, 10), closes)
    result = await _run(bars, OneLowballLimit(), schedule=_nse_schedule(), intraday_square_off=True)
    # The square-off cancels the resting limit (the Worker's cancel_all_working leg) —
    # the post-cutoff crash must NOT fill it. Zero fills, flat book, zero P&L.
    assert result.stats.num_fills == 0
    assert result.stats.final_pnl == 0


async def test_rollover_flattens_a_position_the_thin_tape_stranded() -> None:
    from alpha_core.strategy.examples.placeholder import PlaceholderStrategy

    # Day 1: tape STOPS at a 15:19 close — before the 15:20 square-off instant, so no
    # bar can trigger the cutoff (live's wall-clock periodic would have flattened).
    day1 = _bars(_wed(15, 10), ["100"] * 9)  # closes 15:11..15:19, entry fills at 100
    # Day 2 (Thursday): the first bar must flatten the stranded book at ITS close.
    day2_start = datetime(2026, 6, 18, 9, 15, tzinfo=IST)
    day2 = _bars(day2_start, ["110"] * 5)
    strat = PlaceholderStrategy(quantity=Decimal("2"))
    mis = await _run(day1 + day2, strat, schedule=_nse_schedule(), intraday_square_off=True)
    # entry (1 fill at 100) + the rollover flatten (1 fill at 110); the overnight gap
    # lands in the fold — honest about what an offline tape can know.
    assert mis.stats.num_fills == 2
    assert mis.stats.final_pnl == (Decimal("110") - Decimal("100")) * 2


async def test_daily_bars_with_a_schedule_raise() -> None:
    daily = Bar(
        symbol=SYMBOL,
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        start=_wed(9, 15).astimezone(UTC),
        interval=timedelta(days=1),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1"),
    )
    with pytest.raises(ValueError, match="daily"):
        await _run([daily], EveryBarBuyer(), schedule=_nse_schedule())


def test_degenerate_schedules_are_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="square_off must not precede"):
        MarketSchedule(
            tz="Asia/Kolkata",
            open_time=time(9, 15),
            close_time=time(15, 30),
            no_new_entry=time(15, 15),
            square_off=time(15, 10),  # before no_new_entry — nonsensical interleave
        )
    with pytest.raises(ValueError, match="within"):
        MarketSchedule(
            tz="Asia/Kolkata",
            open_time=time(9, 15),
            close_time=time(15, 30),
            no_new_entry=time(16, 0),  # outside the session
        )


async def test_24x7_book_never_squares_off_even_with_the_flag() -> None:
    # Review #176 R1: a 24x7 schedule + intraday_square_off must NOT flatten at the
    # UTC-midnight rollover — the Worker never squares off a 24x7 book.
    bars = [
        Bar(
            symbol="BTCUSDT",
            venue=Venue.BINANCE,
            asset_class=AssetClass.CRYPTO,
            start=datetime(2026, 6, 17, 23, 57, tzinfo=UTC) + i * ONE_MIN,
            interval=ONE_MIN,
            open=Decimal(str(100 + i)),
            high=Decimal(str(101 + i)),
            low=Decimal(str(99 + i)),
            close=Decimal(str(100 + i)),
            volume=Decimal("1"),
        )
        for i in range(6)  # closes 23:58 .. 00:03 — crosses UTC midnight
    ]
    crypto_costs: dict[str, object] = {
        "slippage": {
            "crypto_perp": {"type": "bps", "value": 0},
            "default_spread": {"crypto_perp": 0},
            "stress_multiplier": 2,
        },
        "segments": {"crypto_perp": {}},
    }

    async def run(flag: bool) -> BacktestResult:
        return await run_backtest(
            bars=bars,
            strategy=EveryBarBuyer(),
            instruments={"BTCUSDT": InstrumentMeta(asset_class=AssetClass.CRYPTO)},
            risk_config=_risk(),
            cost_config=crypto_costs,
            venue=Venue.BINANCE,
            starting_cash=Decimal("1000000"),
            schedule=MarketSchedule(is_24x7=True),
            intraday_square_off=flag,
        )

    with_flag = await run(True)
    without = await run(False)
    assert with_flag.equity_curve == without.equity_curve
    assert with_flag.stats == without.stats


async def test_square_off_flattens_once_at_the_cutoff_not_at_close() -> None:
    # Enter long before the cutoff; prices keep RISING after 15:20. MIS-style
    # square-off must realize at the 15:20 close (200), never ride to 209.
    closes = [str(200 + i) for i in range(15)]  # 15:11 -> 15:25 closes: 200..214
    bars = _bars(_wed(15, 10), closes)
    mis = await _run(bars, EveryBarBuyer(), schedule=_nse_schedule(), intraday_square_off=True)
    cnc = await _run(bars, EveryBarBuyer(), schedule=_nse_schedule(), intraday_square_off=False)
    # Both books hold 4 units from closes 200,201,202,203 (avg 201.5). MIS exits all
    # 4 at the first close >= 15:20 (= 209); CNC's terminal flatten exits at 214.
    assert mis.stats.num_fills == 5 and cnc.stats.num_fills == 5
    assert mis.stats.final_pnl == (Decimal("209") - Decimal("201.5")) * 4
    assert cnc.stats.final_pnl == (Decimal("214") - Decimal("201.5")) * 4
    # once per session date: exactly ONE square-off fill despite 6 post-cutoff bars
    assert mis.stats.traded_notional == cnc.stats.traded_notional - 4 * (
        Decimal("214") - Decimal("209")
    )
