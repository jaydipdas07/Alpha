"""B0.9f — engine bake-off proof (backtest half).

Runs the B0.7 reference contract (``MaCrossover``, the portable
``(bars, params) -> signals`` strategy) end-to-end through the **lifted Vega
engine**: ``run_backtest -> StrategyEngine -> OMS (risk -> order FSM ->
PaperBroker -> StateStore)``. This is the "backtest runs / a trivial strategy
trades" half of the B0.9 Done-when; the Delta-**testnet** live-paper half runs
through the ccxt adapter (keys + network) and is captured in
``docs/bakeoff_b0_9f.md``.

Two things are asserted, both bake-off acceptance signals:
- the contract actually *trades* through the lifted execution path (fills > 0,
  P&L is ``Decimal``), i.e. signals survive risk + the order FSM + the broker;
- the run is **deterministic on injected bar-time** — identical bars give an
  identical result, the engine never reads the wall clock (TEST-1 parity flavor).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alpha_core.backtest.runner import BacktestResult, run_backtest
from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.helpers.config import load_yaml
from alpha_core.risk.limits import RiskConfig
from alpha_core.strategy.examples.ma_crossover import MaCrossover, MaCrossoverConfig

SYMBOL = "BTCUSD"
START = datetime(2026, 6, 25, tzinfo=UTC)
ONE_MIN = timedelta(minutes=1)

# Flat, then a clean rise then fall, so a fast(2) SMA crosses the slow(4) SMA up
# (-> BUY) and later down (-> SELL): the contract enters and exits within the window.
_CLOSES = [
    "30000",
    "30000",
    "30000",
    "30000",
    "30200",
    "30500",
    "30800",
    "31000",
    "30500",
    "30000",
    "29500",
    "29000",
]


def _bars() -> list[Bar]:
    out: list[Bar] = []
    for i, c in enumerate(_CLOSES):
        close = Decimal(c)
        out.append(
            Bar(
                symbol=SYMBOL,
                venue=Venue.DELTA,
                asset_class=AssetClass.CRYPTO,
                start=START + i * ONE_MIN,
                interval=ONE_MIN,
                open=close,
                high=close + Decimal("10"),
                low=close - Decimal("10"),
                close=close,
                volume=Decimal("100"),
            )
        )
    return out


def _risk() -> RiskConfig:
    # Permissive limits so the trivial trades clear the gate — the proof here is
    # that the contract *runs through* the engine, not that risk rejects it (the
    # committed config/risk.yaml + the gate logic are proven by test_risk).
    return RiskConfig.model_validate(
        {
            "base_capital": "100000",
            "limits": {
                "max_gross_exposure": "1.00",
                "max_position_per_instrument": "1.00",
                "max_concurrent_positions": 5,
                "max_order_value": "1.00",
                "max_orders_per_minute": 1000,
                "max_daily_loss_halt": "1.00",
                "max_loss_per_trade": "1.00",
                "per_segment_exposure_cap": "1.00",
            },
        }
    )


async def _run() -> BacktestResult:
    return await run_backtest(
        bars=_bars(),
        strategy=MaCrossover(
            MaCrossoverConfig(fast_period=2, slow_period=4, quantity=Decimal("0.01"))
        ),
        instruments={SYMBOL: InstrumentMeta(asset_class=AssetClass.CRYPTO)},
        risk_config=_risk(),
        cost_config=load_yaml("costs.yaml"),  # the committed crypto cost stack
        venue=Venue.DELTA,
        starting_cash=Decimal("100000"),
    )


async def test_ma_crossover_contract_trades_through_lifted_engine() -> None:
    """The B0.7 contract emits signals the lifted OMS turns into real fills."""
    result = await _run()
    assert result.stats.num_fills >= 2  # a BUY entry and a SELL/flatten exit executed
    assert result.stats.traded_notional > 0
    assert isinstance(result.stats.final_pnl, Decimal)  # money is Decimal, never float
    assert not result.halted  # a clean run, no kill-switch trip


async def test_backtest_is_deterministic_on_injected_time() -> None:
    """Identical bars -> identical result: the engine drives bar-time, never the
    wall clock, so backtest == live on the same data (TEST-1 parity flavor)."""
    first = await _run()
    second = await _run()
    assert first.stats == second.stats
    assert first.equity_curve == second.equity_curve
