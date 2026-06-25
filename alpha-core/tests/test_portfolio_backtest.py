"""R5 — SignalBook, rebalance executor, and the multi-instrument backtester."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alpha_core.backtest.runner import BacktestResult, run_portfolio_backtest
from alpha_core.core.enums import AssetClass, OrderType, Side, Venue
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Signal, TargetExposure, Tick
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.helpers.config import PortfolioConfig
from alpha_core.portfolio.construction import WeightAllocator
from alpha_core.portfolio.rebalance import RebalanceOrder, rebalance_orders
from alpha_core.portfolio.signal_book import SignalBook
from alpha_core.risk.limits import RiskConfig

START = datetime(2026, 6, 15, 3, 45, tzinfo=UTC)
ONE_MIN = timedelta(minutes=1)

COST_CONFIG: dict[str, object] = {
    "slippage": {
        "equity": {"type": "bps", "value": 5},
        "crypto": {"type": "bps", "value": 8},
        "index_option": {"type": "ticks", "value": 2},
        "default_spread": {"equity": 0.0005, "crypto": 0.0008, "index_option_ticks": 1},
        "stress_multiplier": 2,
    },
    "segments": {
        "equity_intraday": {"brokerage": {"pct": 0.0003, "flat": 20, "mode": "min"}},
        "index_option": {"brokerage": {"flat": 20, "mode": "flat"}},
        "crypto": {"trading_fee": {"pct": 0.001, "side": "both"}},
    },
}


def _portfolio_cfg(**alloc: object) -> PortfolioConfig:
    allocation = {
        "method": "equal_weight",
        "top_k": 5,
        "max_weight_per_name": "0.5",
        "gross_cap": "1.0",
    }
    allocation.update(alloc)
    return PortfolioConfig.model_validate(
        {
            "allocation": allocation,
            "rebalance": {
                "no_trade_band": "0.02",
                "turnover_cap": "10.0",
                "min_position_weight": "0.05",
            },
        }
    )


def _risk() -> RiskConfig:
    return RiskConfig.model_validate(
        {
            "base_capital": "1000000",
            "limits": {
                "max_gross_exposure": "1.00",
                "max_position_per_instrument": "0.60",
                "max_concurrent_positions": 10,
                "max_order_value": "0.60",
                "max_orders_per_minute": 100000,
                "max_daily_loss_halt": "0.90",
                "max_loss_per_trade": "1.00",
                "per_segment_exposure_cap": "1.00",
            },
        }
    )


def _target(symbol: str, weight: str) -> TargetExposure:
    return TargetExposure(
        strategy_id="s", symbol=symbol, asset_class=AssetClass.EQUITY, weight=Decimal(weight)
    )


# --- SignalBook ----------------------------------------------------------------


def _sig(symbol: str, side: Side, score: str | None, strat: str = "s1") -> Signal:
    return Signal(
        strategy_id=strat,
        symbol=symbol,
        asset_class=AssetClass.EQUITY,
        side=side,
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
        created_at=START,
        score=None if score is None else Decimal(score),
    )


def test_signal_book_set_clear_flip() -> None:
    book = SignalBook()
    book.update([_sig("A", Side.BUY, "2"), _sig("B", Side.BUY, "1")])
    assert len(book) == 2
    # A scored opposite signal flips A's intent; B unchanged.
    book.update([_sig("A", Side.SELL, "3")])
    intents = {s.symbol: s for s in book.active()}
    assert intents["A"].side is Side.SELL
    # A None-scored signal exits B.
    book.update([_sig("B", Side.SELL, None)])
    assert [s.symbol for s in book.active()] == ["A"]


def test_signal_book_active_is_sorted() -> None:
    book = SignalBook()
    book.update([_sig("Z", Side.BUY, "1"), _sig("A", Side.BUY, "1", strat="s2")])
    assert [(s.strategy_id, s.symbol) for s in book.active()] == [("s1", "Z"), ("s2", "A")]


# --- rebalance_orders ----------------------------------------------------------


def test_rebalance_buys_from_flat() -> None:
    orders = rebalance_orders(
        [_target("A", "0.5")], {}, {"A": Decimal("100")}, Decimal("1000000"), _portfolio_cfg()
    )
    assert orders == [RebalanceOrder("A", Side.BUY, Decimal("5000"))]  # 0.5*1e6/100


def test_rebalance_no_trade_band_skips_small() -> None:
    # current weight 0.49 vs target 0.5 → delta 0.01 < band 0.02 → no order.
    orders = rebalance_orders(
        [_target("A", "0.5")],
        {"A": Decimal("4900")},
        {"A": Decimal("100")},
        Decimal("1000000"),
        _portfolio_cfg(),
    )
    assert orders == []


def test_rebalance_sells_to_reduce() -> None:
    orders = rebalance_orders(
        [_target("A", "0.2")],
        {"A": Decimal("5000")},  # weight 0.5 → reduce to 0.2
        {"A": Decimal("100")},
        Decimal("1000000"),
        _portfolio_cfg(),
    )
    assert orders == [RebalanceOrder("A", Side.SELL, Decimal("3000"))]


def test_rebalance_floors_to_lots_and_skips_missing_price() -> None:
    orders = rebalance_orders(
        [_target("A", "0.5"), _target("B", "0.5")],
        {},
        {"A": Decimal("99.97")},  # B has no price → skipped
        Decimal("1000000"),
        _portfolio_cfg(),
    )
    assert len(orders) == 1 and orders[0].symbol == "A"
    assert orders[0].quantity == (
        Decimal("0.5") * Decimal("1000000") / Decimal("99.97")
    ).to_integral_value(rounding="ROUND_DOWN")


def test_rebalance_skips_held_name_without_price() -> None:
    # A held position whose price is unknown is left out of the current book.
    orders = rebalance_orders([], {"A": Decimal("5000")}, {}, Decimal("1000000"), _portfolio_cfg())
    assert orders == []


def test_rebalance_skips_zero_price_target() -> None:
    orders = rebalance_orders(
        [_target("A", "0.5")], {}, {"A": Decimal("0")}, Decimal("1000000"), _portfolio_cfg()
    )
    assert orders == []


def test_rebalance_drops_subunit_quantity() -> None:
    # delta 0.03 survives the band, but 0.03*1e6/40000 = 0.75 → floors to 0 lots.
    orders = rebalance_orders(
        [_target("A", "0.03")], {}, {"A": Decimal("40000")}, Decimal("1000000"), _portfolio_cfg()
    )
    assert orders == []


# --- multi-instrument backtest -------------------------------------------------


class _OneShotBuy(Strategy):
    """Emits a scored BUY for each symbol on its 2nd bar; nothing after."""

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        n = self._seen.get(bar.symbol, 0) + 1
        self._seen[bar.symbol] = n
        if n == 2:
            return [_sig(bar.symbol, Side.BUY, "1")]
        return []

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []


def _two_symbol_bars() -> list[Bar]:
    out: list[Bar] = []
    for i in range(5):
        for sym, base in (("NSE:A", 100), ("NSE:B", 200)):
            c = Decimal(base + i)
            out.append(
                Bar(
                    symbol=sym,
                    venue=Venue.NSE,
                    asset_class=AssetClass.EQUITY,
                    start=START + i * ONE_MIN,
                    interval=ONE_MIN,
                    open=c,
                    high=c + Decimal("2"),
                    low=c - Decimal("2"),
                    close=c,
                    volume=Decimal("1000"),
                )
            )
    return out


def _instruments() -> dict[str, InstrumentMeta]:
    return {
        "NSE:A": InstrumentMeta(asset_class=AssetClass.EQUITY, tick_size=Decimal("0.05")),
        "NSE:B": InstrumentMeta(asset_class=AssetClass.EQUITY, tick_size=Decimal("0.05")),
    }


async def _run() -> BacktestResult:
    return await run_portfolio_backtest(
        bars=_two_symbol_bars(),
        strategies=[_OneShotBuy()],
        allocator=WeightAllocator(_portfolio_cfg(top_k=2)),
        portfolio_config=_portfolio_cfg(top_k=2),
        instruments=_instruments(),
        risk_config=_risk(),
        cost_config=COST_CONFIG,
        starting_cash=Decimal("1000000"),
    )


@pytest.mark.asyncio
async def test_portfolio_backtest_trades_and_curves() -> None:
    result = await _run()
    assert result.stats.num_fills >= 2  # both names bought
    assert len(result.equity_curve) >= 5
    assert not result.halted


@pytest.mark.asyncio
async def test_portfolio_backtest_deterministic() -> None:
    a = await _run()
    b = await _run()
    assert a.equity_curve == b.equity_curve
    assert a.stats == b.stats


@pytest.mark.asyncio
async def test_portfolio_backtest_halts_on_crash() -> None:
    # Buy both names, then crash the price → mark-to-market loss trips the
    # daily-loss halt; the loop flattens and stops.
    bars: list[Bar] = []
    closes = [100, 100, 50, 5]  # buy at bar index 1, then crash
    for i, px in enumerate(closes):
        for sym in ("NSE:A", "NSE:B"):
            c = Decimal(px)
            bars.append(
                Bar(
                    symbol=sym,
                    venue=Venue.NSE,
                    asset_class=AssetClass.EQUITY,
                    start=START + i * ONE_MIN,
                    interval=ONE_MIN,
                    open=c,
                    high=c + Decimal("1"),
                    low=c - Decimal("1") if c > 1 else c,
                    close=c,
                    volume=Decimal("1000"),
                )
            )
    risk = RiskConfig.model_validate(
        {
            "base_capital": "1000000",
            "limits": {
                "max_gross_exposure": "1.00",
                "max_position_per_instrument": "0.60",
                "max_concurrent_positions": 10,
                "max_order_value": "0.60",
                "max_orders_per_minute": 100000,
                "max_daily_loss_halt": "0.05",  # 5% of base → trips on the crash
                "max_loss_per_trade": "1.00",
                "per_segment_exposure_cap": "1.00",
            },
        }
    )
    result = await run_portfolio_backtest(
        bars=bars,
        strategies=[_OneShotBuy()],
        allocator=WeightAllocator(_portfolio_cfg(top_k=2)),
        portfolio_config=_portfolio_cfg(top_k=2),
        instruments=_instruments(),
        risk_config=risk,
        cost_config=COST_CONFIG,
        starting_cash=Decimal("1000000"),
    )
    assert result.halted


@pytest.mark.asyncio
async def test_portfolio_backtest_empty_bars() -> None:
    result = await run_portfolio_backtest(
        bars=[],
        strategies=[_OneShotBuy()],
        allocator=WeightAllocator(_portfolio_cfg()),
        portfolio_config=_portfolio_cfg(),
        instruments=_instruments(),
        risk_config=_risk(),
        cost_config=COST_CONFIG,
    )
    assert result.stats.num_fills == 0
    assert result.equity_curve == []
