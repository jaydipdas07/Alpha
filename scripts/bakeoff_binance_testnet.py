"""B0.9f — engine bake-off, Track B: drive a strategy onto Binance **testnet**.

Routes a ``MaCrossover`` signal through the **lifted Vega engine** end-to-end onto
a real (testnet) crypto venue:

    MaCrossover -> StrategyEngine -> OMS (risk gate) -> CcxtAdapter -> ccxt -> Binance testnet

then confirms the fill and flattens. This is the live-paper half of the B0.9
Done-when ("a trivial strategy trades on testnet via the lifted engine"); the
backtest half is ``alpha-core/tests/test_bakeoff_b0_9f.py``. The captured run is
in ``docs/bakeoff_b0_9f.md``.

Not a CI test (needs ``BINANCE_TESTNET_*`` keys + network). Run on the Mac CLI:

    uv pip install ccxt   # or: uv sync --extra crypto
    uv run python scripts/bakeoff_binance_testnet.py

**Testnet only.** ``set_sandbox_mode(True)`` forces the Binance sandbox (fake
money); the script never touches live keys or the live gate.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import ccxt.async_support as ccxt

from alpha_core.adapters.crypto_ccxt import CcxtAdapter
from alpha_core.core.enums import AssetClass, OrderType, Side, Venue
from alpha_core.core.models import Bar, Signal
from alpha_core.execution.oms import OMS
from alpha_core.execution.state import StateStore
from alpha_core.helpers.config import RetryConfig
from alpha_core.risk.limits import RiskConfig
from alpha_core.risk.manager import RiskManager
from alpha_core.strategy.engine import StrategyEngine
from alpha_core.strategy.examples.ma_crossover import MaCrossover, MaCrossoverConfig

SYMBOL = "BTC/USDT"
QTY = Decimal("0.001")  # clears Binance spot minNotional (~$10) at BTC ~ $59k

# Permissive limits so the trivial order clears the gate — the proof is that the
# strategy *trades through the engine* onto a live venue, not that risk rejects it.
_RISK = {
    "base_capital": "1000000",
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


def _load_env() -> dict[str, str]:
    """Read KEY=VALUE pairs from the repo-root .env (gitignored; never logged)."""
    env: dict[str, str] = {}
    path = Path(__file__).resolve().parents[1] / ".env"
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _rising_bars() -> list[Bar]:
    """A synthetic rising series so MaCrossover(fast=2, slow=4) deterministically
    emits a BUY (the bars only drive the signal; the order fills at the live price)."""
    start = datetime(2026, 6, 25, tzinfo=UTC)
    minute = timedelta(minutes=1)
    return [
        Bar(
            symbol=SYMBOL,
            venue=Venue.BINANCE,
            asset_class=AssetClass.CRYPTO,
            start=start + i * minute,
            interval=minute,
            open=Decimal(c),
            high=Decimal(c),
            low=Decimal(c),
            close=Decimal(c),
            volume=Decimal("1"),
        )
        for i, c in enumerate(["100", "100", "100", "100", "102", "105"])
    ]


async def main() -> None:
    env = _load_env()
    key, secret = env.get("BINANCE_TESTNET_API_KEY"), env.get("BINANCE_TESTNET_API_SECRET")
    if not (key and secret):
        raise SystemExit("missing BINANCE_TESTNET_API_KEY/_SECRET in .env")

    ex = ccxt.binance({"apiKey": key, "secret": secret, "enableRateLimit": True})
    ex.set_sandbox_mode(True)  # Binance testnet — fake money, never live
    print("=== B0.9f live-paper — Binance TESTNET (sandbox) via the lifted engine ===")
    try:
        last = Decimal(str((await ex.fetch_ticker(SYMBOL))["last"]))
        print(f"[feed]  {SYMBOL} last={last}")

        adapter = CcxtAdapter(
            exchange=ex, venue=Venue.BINANCE, retry=RetryConfig(), streaming=False
        )
        store = StateStore("sqlite:///:memory:")
        store.create_schema()
        oms = OMS(
            adapter=adapter,
            risk=RiskManager(RiskConfig.model_validate(_RISK)),
            store=store,
            venue=Venue.BINANCE,
        )
        engine = StrategyEngine(
            MaCrossover(MaCrossoverConfig(fast_period=2, slow_period=4, quantity=QTY))
        )

        buy: Signal | None = None
        for bar in _rising_bars():
            for sig in engine.process_bar(bar):
                buy = sig
        assert buy is not None and buy.side is Side.BUY, "strategy did not emit a BUY"
        print(
            f"[strat] MaCrossover emitted {buy.side.value} {buy.quantity} {SYMBOL} ({buy.reason})"
        )

        order = await oms.submit_signal(buy, reference_price=last)
        assert order is not None, "risk gate rejected the order"
        print(
            f"[oms]   risk-approved -> placed: client_order_id={order.client_order_id} "
            f"venue_order_id={order.venue_order_id} state={order.state.value}"
        )

        await asyncio.sleep(1.0)
        filled = await ex.fetch_order(order.venue_order_id, SYMBOL)
        got = Decimal(str(filled.get("filled") or "0"))
        fee = filled.get("fee") or {}
        print(
            f"[fill]  testnet order {filled.get('status')}: filled={got} "
            f"avg={filled.get('average') or filled.get('price')} "
            f"fee={fee.get('cost')} {fee.get('currency')}"
        )

        # flatten through the OMS too: SELL the net BTC received (testnet stays clean)
        fee_btc = Decimal(str(fee.get("cost") or 0)) if fee.get("currency") == "BTC" else Decimal(0)
        net = (got - fee_btc).quantize(Decimal("0.00001"), rounding=ROUND_DOWN)
        if net > 0:
            sell = Signal(
                strategy_id="flatten",
                symbol=SYMBOL,
                asset_class=AssetClass.CRYPTO,
                side=Side.SELL,
                quantity=net,
                order_type=OrderType.MARKET,
                created_at=datetime.now(UTC),
                reason="flatten the live-paper position",
            )
            sold_order = await oms.submit_signal(sell, reference_price=last)
            assert sold_order is not None
            await asyncio.sleep(1.0)
            sold = await ex.fetch_order(sold_order.venue_order_id, SYMBOL)
            print(f"[flat]  closed {net} BTC via OMS -> {sold.get('status')}")
        print("=== done — a trivial strategy traded on Binance testnet via the lifted engine ===")
    finally:
        await ex.close()


if __name__ == "__main__":
    asyncio.run(main())
