"""Perp funding (R13) — model + backtest P&L + the daily-loss kill gate."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alpha_core.backtest.runner import BacktestResult, run_backtest
from alpha_core.core.enums import AssetClass, OrderType, Side, Venue
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Position, Signal, Tick
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.execution.funding import FundingConfig, funding_cash_flow, load_funding_config
from alpha_core.helpers.config import load_yaml
from alpha_core.risk.limits import RiskConfig

SYM = "BTCUSDT"
START = datetime(2026, 6, 26, tzinfo=UTC)  # a UTC midnight (on the 8h funding grid)
HOUR = timedelta(hours=1)


def _pos(qty: str) -> Position:
    return Position(
        symbol=SYM,
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        quantity=Decimal(qty),
        average_price=Decimal("30000"),
        last_price=Decimal("30000"),
        updated_at=START,
    )


# --- the funding model (pure) -------------------------------------------------
def test_long_pays_funding_when_rate_positive() -> None:
    assert funding_cash_flow(_pos("1"), Decimal("30000"), Decimal("0.0001")) == Decimal("-3")


def test_short_receives_funding_when_rate_positive() -> None:
    assert funding_cash_flow(_pos("-1"), Decimal("30000"), Decimal("0.0001")) == Decimal("3")


def test_load_funding_config() -> None:
    cfg = load_funding_config(load_yaml("costs.yaml"))
    assert cfg is not None and cfg.interval_hours == 8 and cfg.rate == Decimal("0.0001")
    assert load_funding_config({}) is None  # no funding block -> None


# --- backtest integration -----------------------------------------------------
class _BuyAndHold(Strategy):
    """Open one long on the first bar, then hold — so funding accrues on it."""

    def __init__(self, qty: Decimal) -> None:
        self._qty = qty
        self._opened = False

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        if self._opened:
            return []
        self._opened = True
        return [
            Signal(
                strategy_id="hold",
                symbol=bar.symbol,
                asset_class=bar.asset_class,
                side=Side.BUY,
                quantity=self._qty,
                order_type=OrderType.MARKET,
                created_at=bar.start + bar.interval,
            )
        ]

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []


def _flat_bars(hours: int) -> list[Bar]:
    px = Decimal("30000")
    return [
        Bar(
            symbol=SYM,
            venue=Venue.BINANCE,
            asset_class=AssetClass.CRYPTO,
            start=START + i * HOUR,
            interval=HOUR,
            open=px,
            high=px,
            low=px,
            close=px,
            volume=Decimal("1"),
        )
        for i in range(hours)
    ]


def _risk(max_daily_loss: str) -> dict[str, object]:
    return {
        "base_capital": "100000",
        "limits": {
            "max_gross_exposure": "1.00",
            "max_position_per_instrument": "1.00",
            "max_concurrent_positions": 5,
            "max_order_value": "1.00",
            "max_orders_per_minute": 1000,
            "max_daily_loss_halt": max_daily_loss,
            "max_loss_per_trade": "1.00",
            "per_segment_exposure_cap": "1.00",
        },
    }


async def _run(*, rate: str, max_daily_loss: str, starting_cash: str) -> BacktestResult:
    return await run_backtest(
        bars=_flat_bars(48),  # 2 days -> crosses six 8h funding boundaries
        strategy=_BuyAndHold(Decimal("1")),
        instruments={SYM: InstrumentMeta(asset_class=AssetClass.CRYPTO)},
        risk_config=RiskConfig.model_validate(_risk(max_daily_loss)),
        cost_config=load_yaml("costs.yaml"),
        venue=Venue.BINANCE,
        starting_cash=Decimal(starting_cash),
        funding=FundingConfig(interval_hours=8, rate=Decimal(rate)),
    )


async def test_funding_shows_in_backtest_pnl() -> None:
    result = await _run(rate="0.0001", max_daily_loss="1.00", starting_cash="1000000")
    assert result.stats.funding_paid < 0  # a held long pays funding (rate > 0)
    assert not result.halted  # a light rate doesn't trip the gate


async def test_funding_bleed_trips_the_loss_gate() -> None:
    result = await _run(rate="0.05", max_daily_loss="0.02", starting_cash="100000")
    assert result.halted  # the funding-bleed tripped the daily-loss kill (R13)
    assert result.stats.funding_paid <= Decimal("-2000")  # accrued past the loss cap
