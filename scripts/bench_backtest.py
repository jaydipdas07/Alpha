"""Full-pipeline backtest throughput benchmark.

Measures how fast the event-driven backtester replays minute bars through the SAME
path a live run takes (ADR 0001 parity): StrategyEngine -> OMS (risk gate -> order
FSM -> PaperBroker -> StateStore) -> fills -> Decimal P&L fold + per-bar mark.
Bar generation is excluded from the timed region; the number is the engine, not
the data prep.

The tape is a seeded random walk, so runs are reproducible and the MA-crossover
reference strategy trades at a realistic cadence (decision-only bars mixed with
order-placing bars). Reports bars/sec and the mean per-bar decision-loop latency.

Usage:
    uv run python scripts/bench_backtest.py [--bars 200000] [--repeat 3] [--seed 42]
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alpha_core.backtest.runner import BacktestResult, run_backtest
from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.observability.logging import configure_logging
from alpha_core.risk.limits import RiskConfig
from alpha_core.strategy.examples.ma_crossover import MaCrossover

SYMBOL = "NSE:BENCH"
START = datetime(2026, 1, 1, tzinfo=UTC)
ONE_MIN = timedelta(minutes=1)

# The test-suite cost/risk shapes (test_backtest.py) — representative, not tuned.
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

RISK = RiskConfig.model_validate(
    {
        "base_capital": "100000",
        "limits": {
            "max_gross_exposure": "1.00",
            "max_position_per_instrument": "0.50",
            "max_concurrent_positions": 5,
            "max_order_value": "0.50",
            "max_orders_per_minute": 1000,
            "max_daily_loss_halt": "0.50",
            "max_loss_per_trade": "0.05",
            "per_segment_exposure_cap": "1.00",
        },
    }
)


def make_bars(n: int, seed: int) -> list[Bar]:
    """A seeded random-walk minute tape (positive prices, tick-size 0.05)."""
    rng = random.Random(seed)
    bars: list[Bar] = []
    price = Decimal("1000.00")
    tick = Decimal("0.05")
    for i in range(n):
        step = tick * rng.randint(-10, 10)
        close = max(price + step, tick)
        high = max(price, close) + tick
        low = min(price, close) - tick
        bars.append(
            Bar(
                symbol=SYMBOL,
                venue=Venue.NSE,
                asset_class=AssetClass.EQUITY,
                start=START + i * ONE_MIN,
                interval=ONE_MIN,
                open=price,
                high=high,
                low=max(low, tick),
                close=close,
                volume=Decimal("1000"),
            )
        )
        price = close
    return bars


async def run_once(bars: list[Bar]) -> tuple[float, BacktestResult]:
    """One timed replay; returns (wall seconds, result)."""
    strategy = MaCrossover()
    instruments = {SYMBOL: InstrumentMeta(asset_class=AssetClass.EQUITY)}
    t0 = time.perf_counter()
    result = await run_backtest(
        bars=bars,
        strategy=strategy,
        instruments=instruments,
        risk_config=RISK,
        cost_config=COST_CONFIG,
        venue=Venue.NSE,
        starting_cash=Decimal("100000"),
    )
    return time.perf_counter() - t0, result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=int, default=200_000, help="minute bars per run")
    parser.add_argument("--repeat", type=int, default=3, help="runs (best is reported)")
    parser.add_argument("--seed", type=int, default=42, help="random-walk seed")
    args = parser.parse_args()

    # Per-fill INFO logging is I/O inside the timed region — silence it so the
    # number measures the engine, not the terminal.
    configure_logging("WARNING")

    bars = make_bars(args.bars, args.seed)
    span_days = args.bars / (24 * 60)
    print(f"tape: {args.bars:,} x 1m bars (~{span_days:.0f} days), seed {args.seed}")

    best: float | None = None
    for i in range(args.repeat):
        wall, result = asyncio.run(run_once(bars))
        rate = len(bars) / wall
        print(
            f"run {i + 1}/{args.repeat}: {wall:.2f}s -> {rate:,.0f} bars/s "
            f"({wall / len(bars) * 1e6:.0f} us/bar), fills={result.stats.num_fills}, "
            f"halted={result.halted}"
        )
        best = wall if best is None else min(best, wall)

    assert best is not None
    rate = len(bars) / best
    print(
        f"\nbest: {rate:,.0f} bars/s through the full signal->risk->order->fill->P&L "
        f"pipeline ({best / len(bars) * 1e6:.0f} us/bar mean decision-loop latency)"
    )


if __name__ == "__main__":
    main()
