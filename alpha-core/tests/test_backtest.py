"""Backtest runner + stats tests (Phase 7 B7.1/B7.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alpha_core.backtest.runner import BacktestResult, run_backtest
from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.risk.limits import RiskConfig
from alpha_core.strategy.examples.placeholder import PlaceholderStrategy

SYMBOL = "NSE:RELIANCE"
START = datetime(2026, 6, 15, 3, 45, tzinfo=UTC)
ONE_MIN = timedelta(minutes=1)

COST_CONFIG: dict[str, object] = {
    "slippage": {
        "equity": {"type": "bps", "value": 5},
        "crypto_perp": {"type": "bps", "value": 8},
        "index_option": {"type": "ticks", "value": 2},
        "default_spread": {"equity": 0.0005, "crypto_perp": 0.0008, "index_option_ticks": 1},
        "stress_multiplier": 2,
    },
    "segments": {
        "equity_intraday": {"brokerage": {"pct": 0.0003, "flat": 20, "mode": "min"}},
        "index_option": {"brokerage": {"flat": 20, "mode": "flat"}},
        "crypto_perp": {"trading_fee": {"pct": 0.001, "side": "both"}},
    },
}


def _risk() -> RiskConfig:
    return RiskConfig.model_validate(
        {
            "base_capital": "100000",
            "limits": {
                "max_gross_exposure": "1.00",
                "max_position_per_instrument": "0.50",
                "max_concurrent_positions": 5,
                "max_order_value": "0.50",
                "max_orders_per_minute": 1000,
                "max_daily_loss_halt": "0.50",
                "max_loss_per_trade": "0.01",
                "per_segment_exposure_cap": "1.00",
            },
        }
    )


def _bars(closes: list[str]) -> list[Bar]:
    out: list[Bar] = []
    for i, c in enumerate(closes):
        close = Decimal(c)
        out.append(
            Bar(
                symbol=SYMBOL,
                venue=Venue.NSE,
                asset_class=AssetClass.EQUITY,
                start=START + i * ONE_MIN,
                interval=ONE_MIN,
                open=close - Decimal("1"),
                high=max(close, close) + Decimal("2"),
                low=close - Decimal("2"),
                close=close,
                volume=Decimal("1000"),
            )
        )
    return out


def _instruments() -> dict[str, InstrumentMeta]:
    return {SYMBOL: InstrumentMeta(asset_class=AssetClass.EQUITY)}


async def _run(closes: list[str], *, stress: bool = False) -> BacktestResult:
    return await run_backtest(
        bars=_bars(closes),
        strategy=PlaceholderStrategy(quantity=Decimal("10")),
        instruments=_instruments(),
        risk_config=_risk(),
        cost_config=COST_CONFIG,
        starting_cash=Decimal("1000000"),
        stress=stress,
    )


async def test_profit_on_rising_series() -> None:
    res = await _run([str(100 + i) for i in range(20)])  # +1/bar
    assert res.stats.num_fills == 2  # entry + square-off
    assert res.stats.final_pnl > 0
    assert res.stats.win_rate == 1.0  # the single closed trade won
    assert not res.halted


async def test_loss_on_falling_series() -> None:
    res = await _run([str(120 - i) for i in range(20)])  # -1/bar
    assert res.stats.final_pnl < 0
    assert res.stats.win_rate == 0.0


async def test_max_drawdown_positive_on_round_trip() -> None:
    # up then down -> a peak then a trough -> positive drawdown
    res = await _run([str(100 + i) for i in range(10)] + [str(110 - i) for i in range(10)])
    assert res.stats.max_drawdown_pct > 0


async def test_stress_reduces_pnl() -> None:
    closes = [str(100 + i) for i in range(20)]
    base = await _run(closes, stress=False)
    stressed = await _run(closes, stress=True)
    # 2x slippage -> worse fills -> lower realized P&L (ADR 0008)
    assert stressed.stats.final_pnl < base.stats.final_pnl


async def test_report_renders() -> None:
    res = await _run([str(100 + i) for i in range(5)])
    text = res.render()
    assert "backtest" in text
    assert "Sharpe" in text


async def test_empty_bars() -> None:
    res = await run_backtest(
        bars=[],
        strategy=PlaceholderStrategy(),
        instruments=_instruments(),
        risk_config=_risk(),
        cost_config=COST_CONFIG,
    )
    assert res.stats.num_fills == 0
    assert res.stats.final_pnl == 0
