"""Worker run-loop integration tests (M3.1/M3.2/M3.4).

Drives the real Worker (OMS + risk + reconcile + BarBuilder + strategy engine)
over a scripted tick feed via a fake venue that both yields ticks and fills
orders — proving the live wiring: ticks -> bars -> strategy -> submit -> fills ->
derived P&L -> heartbeat, plus stop and halt-flatten.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alpha_core.core.enums import AssetClass, OrderType, Side, Venue
from alpha_core.core.interfaces import BrokerAdapter, BrokerEventKind, BrokerOrderEvent, Strategy
from alpha_core.core.models import Bar, Fill, Order, Position, Signal, Tick
from alpha_core.data.bar_builder import BarBuilder
from alpha_core.data.feed import AdapterFeed
from alpha_core.execution.commands import RunState, WorkerControl
from alpha_core.execution.deadman import HeartbeatFile
from alpha_core.execution.oms import OMS
from alpha_core.execution.reconcile import Reconciler
from alpha_core.execution.state import StateStore
from alpha_core.risk.limits import RiskConfig
from alpha_core.risk.manager import KillTrigger, RiskManager
from alpha_core.scheduler.clock import FakeClock
from alpha_core.strategy.engine import StrategyEngine
from worker.config import EnvConfig
from worker.loop import Worker

T0 = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)
NOW = T0 + timedelta(minutes=1)  # the worker's wall-clock instant (after the scripted ticks)
SYMBOL = "BTC/USDT"


class _AlwaysBuy(Strategy):
    """Buys a fixed quantity on every closed bar."""

    def __init__(self, qty: str = "0.01") -> None:
        self._qty = Decimal(qty)

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        return [
            Signal(
                strategy_id="test",
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


class _FakeVenue(BrokerAdapter):
    """Yields a scripted tick stream AND fills every order at the latest tick price."""

    def __init__(self, ticks: list[Tick]) -> None:
        self._ticks = ticks
        self._price = Decimal("100")
        self._pending: list[Fill] = []
        self.placed: list[Order] = []

    async def place_order(self, order: Order) -> str:
        vid = f"V{len(self.placed)}"
        self.placed.append(order)
        self._pending.append(
            Fill(
                fill_id=f"F{len(self.placed)}",
                client_order_id=order.client_order_id,
                venue_order_id=vid,
                symbol=order.symbol,
                venue=order.venue,
                asset_class=order.asset_class,
                side=order.side,
                quantity=order.quantity,
                price=self._price,
                fees=Decimal("0"),
                ts=order.created_at,
            )
        )
        return vid

    async def cancel(self, client_order_id: str) -> None:
        return None

    async def modify(self, client_order_id: str, **kwargs: object) -> None:
        raise NotImplementedError

    async def get_orders(self) -> list[Order]:
        return []

    async def get_positions(self) -> list[Position]:
        return []

    async def find_order_id(self, client_order_id: str) -> str | None:
        return None

    async def subscribe_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        for tick in self._ticks:
            self._price = tick.last_price or self._price
            yield tick

    async def order_events(self) -> AsyncIterator[BrokerOrderEvent]:
        while self._pending:
            fill = self._pending.pop(0)
            yield BrokerOrderEvent(
                kind=BrokerEventKind.FILL,
                client_order_id=fill.client_order_id,
                venue_order_id=fill.venue_order_id,
                fill=fill,
            )


def _risk() -> RiskManager:
    return RiskManager(
        RiskConfig.model_validate(
            {
                "base_capital": "100000",
                "currency": "USDT",
                "limits": {
                    "max_gross_exposure": "1.00",
                    "max_position_per_instrument": "0.50",
                    "max_concurrent_positions": 5,
                    "max_order_value": "0.50",
                    "max_orders_per_minute": 100,
                    "max_daily_loss_halt": "0.10",
                    "max_loss_per_trade": "0.05",
                    "per_segment_exposure_cap": "1.00",
                },
            }
        )
    )


def _env(tmp_path: object) -> EnvConfig:
    return EnvConfig.model_validate(
        {
            "env": "paper",
            "mode": "paper",
            "allow_live": False,
            "worker_id": "w-test",
            "venue": "binance-spot-testnet",
            "strategy": "idle",  # the engine is injected directly in the test
            "symbols": [SYMBOL],
            "bar_interval_seconds": 1,
            "state_db": "sqlite:///:memory:",
            "heartbeat_path": f"{tmp_path}/hb",
            "command_poll_seconds": 0.01,
            # Large so the periodic reconcile doesn't fire during the bounded market
            # loop (the fake venue reports no broker truth; reconcile is tested in
            # test_reconcile). The periodic task still starts + is cancelled on exit.
            "reconcile_interval_seconds": 3600,
        }
    )


def _ticks(prices: list[str]) -> list[Tick]:
    return [
        Tick(
            symbol=SYMBOL,
            venue=Venue.BINANCE,
            asset_class=AssetClass.CRYPTO,
            ts=T0 + timedelta(seconds=i),
            last_price=Decimal(p),
            volume=Decimal("1"),
        )
        for i, p in enumerate(prices)
    ]


def _worker(
    tmp_path: object,
    ticks: list[Tick],
    *,
    strategy: Strategy,
    risk: RiskManager | None = None,
    venue: _FakeVenue | None = None,
    drain_inline: bool = True,
) -> tuple[Worker, _FakeVenue, OMS, HeartbeatFile]:
    risk = risk or _risk()
    store = StateStore("sqlite:///:memory:")
    store.create_schema()
    venue = venue or _FakeVenue(ticks)
    clock = FakeClock(NOW)
    oms = OMS(adapter=venue, risk=risk, store=store, venue=Venue.BINANCE, clock=clock)
    heartbeat = HeartbeatFile(f"{tmp_path}/hb")
    worker = Worker(
        env=_env(tmp_path),
        adapter=venue,
        oms=oms,
        risk=risk,
        reconciler=Reconciler(adapter=venue, risk=risk),
        engine=StrategyEngine(strategy),
        bar_builder=BarBuilder(1),
        heartbeat=heartbeat,
        control=WorkerControl(),
        feed=AdapterFeed(venue),
        feed_stale_seconds=600,
        drain_inline=drain_inline,  # bounded fake feed -> drain fills inline
        clock=clock,
    )
    return worker, venue, oms, heartbeat


async def test_loop_builds_bars_submits_fills_and_derives_pnl(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # 4 ticks across 4 seconds -> 3 closed 1s bars -> 3 buys. Price holds at 100.
    worker, venue, oms, heartbeat = _worker(
        tmp_path, _ticks(["100", "100", "100", "100"]), strategy=_AlwaysBuy("0.01")
    )
    await worker.run()
    assert len(venue.placed) == 3  # one buy per closed bar
    pos = oms.positions[0]
    assert pos.quantity == Decimal("0.03")  # all three fills booked (derived from fills, R11)
    assert pos.average_price == Decimal("100")
    assert heartbeat.last_beat() is not None  # liveness was written


async def test_stop_control_ends_the_loop(tmp_path) -> None:  # type: ignore[no-untyped-def]
    worker, venue, _oms, _hb = _worker(
        tmp_path, _ticks(["100", "100", "100"]), strategy=_AlwaysBuy()
    )
    worker._control.set(RunState.STOPPED)  # already told to stop
    await worker.run()
    assert venue.placed == []  # the market loop exits before trading


async def test_halt_flattens_the_book_and_pauses(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Build a position, then latch the kill: _maybe_flatten_on_halt must flatten + pause.
    worker, _venue, oms, _hb = _worker(
        tmp_path, _ticks(["100", "100"]), strategy=_AlwaysBuy("0.02")
    )
    await worker.start()
    # one bar -> one buy
    for tick in _ticks(["100", "100"]):
        bar = worker._bars.add(tick)
        if bar is not None:
            for sig in worker._engine.process_bar(bar):
                await oms.submit_signal(sig, reference_price=bar.close)
            await oms.drain_events()
    assert oms.positions[0].quantity == Decimal("0.02")

    worker._risk.trip(KillTrigger.MANUAL)
    await worker._maybe_flatten_on_halt()
    assert worker._control.state is RunState.PAUSED
    await oms.drain_events()
    assert all(p.quantity == 0 for p in oms.positions)  # flattened


def _pos(symbol: str, qty: str) -> Position:
    return Position(
        venue=Venue.BINANCE,
        symbol=symbol,
        asset_class=AssetClass.CRYPTO,
        quantity=Decimal(qty),
        average_price=Decimal("100"),
        last_price=Decimal("100"),
        updated_at=T0,
    )


async def test_check_feed_stale_trips_the_kill(tmp_path) -> None:  # type: ignore[no-untyped-def]
    worker, *_ = _worker(tmp_path, _ticks(["100"]), strategy=_AlwaysBuy())
    worker._feed_stale_seconds = 5
    worker._check_feed_stale()  # no tick yet -> no trip (early return)
    assert worker._risk.is_halted is False
    worker._last_tick_at = NOW - timedelta(seconds=30)  # last tick 30s ago > 5s budget
    worker._check_feed_stale()
    assert worker._risk.is_halted and worker._risk.halt_trigger is KillTrigger.FEED_STALE


async def test_periodic_reconciles_and_beats_once(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Drive exactly one periodic cycle (stub sleep to stop the loop after it).
    worker, _venue, _oms, heartbeat = _worker(tmp_path, _ticks(["100"]), strategy=_AlwaysBuy())
    worker._last_tick_at = NOW  # fresh -> no feed-stale trip

    async def _sleep_then_stop(_seconds: float) -> None:
        worker._control.set(RunState.STOPPED)  # exit the while-loop after this iteration

    monkeypatch.setattr("worker.loop.asyncio.sleep", _sleep_then_stop)
    await worker._periodic()
    assert heartbeat.last_beat() is not None  # beat in the periodic cycle
    assert worker._risk.is_halted is False  # clean reconcile (flat book)


async def test_reconcile_adopts_clean_then_halts_on_drift(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Broker (fake) reports a position the local book lacks -> unexplained drift -> halt.
    venue = _FakeVenue([])

    async def _positions() -> list[Position]:
        return [_pos(SYMBOL, "1")]

    venue.get_positions = _positions  # type: ignore[method-assign]
    worker, _v, _oms, _hb = _worker(tmp_path, [], strategy=_AlwaysBuy(), venue=venue)
    await worker._reconcile()
    assert worker._risk.is_halted is True  # broker-vs-local drift halts (TEST-7)


async def test_startup_reconcile_not_clean_pauses(tmp_path) -> None:  # type: ignore[no-untyped-def]
    venue = _FakeVenue([])

    async def _positions() -> list[Position]:
        return [_pos(SYMBOL, "1")]  # broker holds a position we don't -> not clean

    venue.get_positions = _positions  # type: ignore[method-assign]
    worker, _v, _oms, _hb = _worker(tmp_path, [], strategy=_AlwaysBuy(), venue=venue)
    await worker.start()
    assert worker._control.state is RunState.PAUSED
    assert worker._risk.is_halted is True


async def test_consume_events_books_streamed_fills(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # The streaming path (drain_inline=False): consume_events books queued fills.
    worker, _venue, oms, _hb = _worker(
        tmp_path, _ticks(["100"]), strategy=_AlwaysBuy(), drain_inline=False
    )
    sig = _AlwaysBuy("0.01").on_bar(
        Bar(
            symbol=SYMBOL,
            venue=Venue.BINANCE,
            asset_class=AssetClass.CRYPTO,
            start=T0,
            interval=timedelta(seconds=1),
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=Decimal("1"),
        )
    )[0]
    await oms.submit_signal(sig, reference_price=Decimal("100"))
    await worker._consume_events()  # the fake's order_events ends when drained
    assert oms.positions[0].quantity == Decimal("0.01")


async def test_build_worker_constructs_from_paper_config(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from worker.loop import build_worker

    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "k")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "s")
    env = EnvConfig.model_validate(
        {
            "env": "paper",
            "mode": "paper",
            "allow_live": False,
            "worker_id": "w",
            "venue": "binance-spot-testnet",
            "strategy": "idle",
            "symbols": [SYMBOL],
            "bar_interval_seconds": 60,
            "state_db": "sqlite:///:memory:",
            "heartbeat_path": f"{tmp_path}/hb",
            "command_poll_seconds": 1.0,
            "reconcile_interval_seconds": 30,
        }
    )
    worker = build_worker(env)  # builds a real (sandbox) ccxt adapter
    try:
        assert isinstance(worker, Worker)
    finally:
        await worker._adapter.aclose()
