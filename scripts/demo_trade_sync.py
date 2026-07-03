"""Demo deployment — LIVE public ticks → PaperBroker fills → the REAL pod trade sync.

Exercises PR #157 end-to-end with zero venue risk and zero keys: real-time public
Binance trade prints (market data only, unauthenticated) drive the SAME Worker loop the
box runs — BarBuilder → MaCrossover → risk gate → OMS — with the PaperBroker executing
simulated fills through the cost model, and PodTradeSync pushing orders/fills/positions/
pnl_snapshots to the live Vault pod (deployment from ``config/demo.yaml``). This is the
paper-trading assembly (live feed + simulated execution) M4.5 reuses.

Run (LEMMA_TOKEN must be in the environment or .env):

    uv run python scripts/demo_trade_sync.py [seconds]
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Sequence
from decimal import Decimal

import ccxt.pro as ccxtpro

from alpha_core.adapters.crypto_ccxt import CcxtAdapter
from alpha_core.adapters.paper import PaperBroker
from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.interfaces import DataFeed
from alpha_core.core.models import Bar, Tick
from alpha_core.data.bar_builder import BarBuilder
from alpha_core.data.feed import AdapterFeed
from alpha_core.execution.commands import RunState, WorkerControl
from alpha_core.execution.costs import CostModel, InstrumentMeta
from alpha_core.execution.deadman import HeartbeatFile
from alpha_core.execution.oms import OMS
from alpha_core.execution.reconcile import Reconciler
from alpha_core.execution.state import StateStore
from alpha_core.helpers.config import load_yaml
from alpha_core.observability.logging import get_logger
from alpha_core.risk.limits import load_risk_config
from alpha_core.risk.manager import RiskManager
from alpha_core.scheduler.clock import SystemClock
from alpha_core.strategy.engine import StrategyEngine
from alpha_core.strategy.examples.ma_crossover import MaCrossover, MaCrossoverConfig
from worker.config import load_dotenv, load_env_config
from worker.loop import Worker
from worker.pod_sync import PodStatusWriter, PodTradeSync, build_pod_client

# Demo-run tunables (a THROWAWAY exercise harness, not a deployable: quick crosses +
# a paper-sized clip; the deployable path stays config-only per CLAUDE.md).
FAST, SLOW = 6, 18
QUANTITY = Decimal("0.001")  # ~1e-3 BTC — comfortably inside the fat-finger cap
STARTING_CASH = Decimal("1000000")
DEFAULT_SECONDS = 480.0


class TeeFeed(DataFeed):
    """Yield the inner feed's ticks, teeing each into the PaperBroker's market view
    (the simulated venue needs quotes to price fills — the backtester does the same)."""

    def __init__(self, inner: DataFeed, paper: PaperBroker) -> None:
        self._inner = inner
        self._paper = paper

    async def stream_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        async for tick in self._inner.stream_ticks(symbols):
            self._paper.on_tick(tick)
            yield tick

    def stream_bars(self, symbols: Sequence[str]) -> AsyncIterator[Bar]:
        raise NotImplementedError("ticks only; the worker's BarBuilder makes the bars")


async def main(seconds: float) -> None:
    log = get_logger("demo")
    load_dotenv()
    env = load_env_config("demo")

    # Market data: PUBLIC Binance trade prints (mainnet, unauthenticated, read-only).
    ex = ccxtpro.binance()
    data = CcxtAdapter(exchange=ex, venue=Venue.BINANCE, streaming=True)

    paper = PaperBroker(
        cost_model=CostModel(load_yaml("costs.yaml")),
        instruments={s: InstrumentMeta(asset_class=AssetClass.CRYPTO) for s in env.symbols},
        starting_cash=STARTING_CASH,
    )
    risk = RiskManager(load_risk_config())
    store = StateStore(env.state_db)
    store.create_schema()
    clock = SystemClock()
    oms = OMS(adapter=paper, risk=risk, store=store, venue=Venue.PAPER, clock=clock)
    control = WorkerControl()

    pod = build_pod_client(env)
    if pod is None or env.pod_sync is None or not env.pod_sync.deployment_id:
        raise SystemExit("pod sync unconfigured — the demo exists to exercise it")
    worker = Worker(
        env=env,
        adapter=paper,
        oms=oms,
        risk=risk,
        reconciler=Reconciler(adapter=paper, risk=risk),
        engine=StrategyEngine(
            MaCrossover(MaCrossoverConfig(fast_period=FAST, slow_period=SLOW, quantity=QUANTITY))
        ),
        bar_builder=BarBuilder(env.bar_interval_seconds),
        heartbeat=HeartbeatFile(env.heartbeat_path),
        control=control,
        feed=TeeFeed(AdapterFeed(data), paper),
        feed_stale_seconds=600.0,
        drain_inline=True,  # PaperBroker's order_events is BOUNDED — drain per bar
        pod_status=PodStatusWriter(pod, worker_id=env.worker_id, mode=env.mode),
        pod_trade_sync=PodTradeSync(
            pod,
            deployment_id=env.pod_sync.deployment_id,
            snapshot_seconds=env.pod_sync.pnl_snapshot_seconds,
        ),
        clock=clock,
    )

    async def _stop_after() -> None:
        await asyncio.sleep(seconds)
        log.info("demo_window_over", seconds=seconds)
        control.set(RunState.STOPPED)

    stopper = asyncio.create_task(_stop_after())
    try:
        await worker.run()
    finally:
        stopper.cancel()
        await data.aclose()
        log.info(
            "demo_done",
            realized=str(oms.total_realized_pnl()),
            unrealized=str(oms.total_unrealized_pnl()),
            positions={p.symbol: str(p.quantity) for p in oms.positions},
            orders=len(oms.all_orders()),
            fills=len(oms.all_fills()),
        )


if __name__ == "__main__":
    asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SECONDS))
