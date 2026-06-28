"""Worker run-loop integration tests (M3.1/M3.2/M3.4).

Drives the real Worker (OMS + risk + reconcile + BarBuilder + strategy engine)
over a scripted tick feed via a fake venue that both yields ticks and fills
orders — proving the live wiring: ticks -> bars -> strategy -> submit -> fills ->
derived P&L -> heartbeat, plus stop and halt-flatten.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

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
from worker.pod_sync import PodStatusWriter

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


class _RecordingPodStatus:
    """A fake PodStatusWriter that records the heartbeat + risk-event calls (structural)."""

    def __init__(self) -> None:
        self.beats: list[dict[str, Any]] = []
        self.risk_events: list[dict[str, Any]] = []

    async def beat(
        self, *, now: datetime, armed: bool, positions: Sequence[Position], detail: dict[str, Any]
    ) -> None:
        self.beats.append({"armed": armed, "positions": list(positions), "detail": detail})

    async def record_risk_event(
        self, *, kind: str, severity: str, now: datetime, detail: dict[str, Any]
    ) -> None:
        self.risk_events.append({"kind": kind, "severity": severity, "detail": detail})


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
    pod_status: PodStatusWriter | None = None,
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
        pod_status=pod_status,
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


async def test_retrip_after_clear_halt_flattens_again(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # BLOCKER 2: after a clear_halt re-arm + a NEW position, a fresh trip must flatten
    # again — the generation guard must not leave the second kill un-flattened.
    worker, _venue, oms, _hb = _worker(
        tmp_path, _ticks(["100", "100"]), strategy=_AlwaysBuy("0.02")
    )
    await worker.start()
    sig = Signal(
        strategy_id="t",
        symbol=SYMBOL,
        asset_class=AssetClass.CRYPTO,
        side=Side.BUY,
        quantity=Decimal("0.02"),
        order_type=OrderType.MARKET,
        created_at=NOW,
    )
    await oms.submit_signal(sig, reference_price=Decimal("100"))
    await oms.drain_events()
    worker._risk.trip(KillTrigger.MANUAL)  # first kill (generation 1)
    await worker._maybe_flatten_on_halt()
    await oms.drain_events()
    assert all(p.quantity == 0 for p in oms.positions)

    worker._risk.rearm()  # clear_halt
    worker._control.set(RunState.RUNNING)
    await oms.submit_signal(
        sig.model_copy(update={"quantity": Decimal("0.03")}), reference_price=Decimal("100")
    )
    await oms.drain_events()
    assert oms.positions[0].quantity == Decimal("0.03")  # a fresh position
    worker._risk.trip(KillTrigger.MANUAL)  # re-trip (generation 2)
    await worker._maybe_flatten_on_halt()
    await oms.drain_events()
    assert all(p.quantity == 0 for p in oms.positions)  # flattened AGAIN (not skipped)


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


def test_ensure_db_dir_creates_parent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from worker.loop import _ensure_db_dir

    target = tmp_path / "state" / "alpha.db"
    _ensure_db_dir(f"sqlite:///{target}")  # the `state/` dir doesn't exist yet
    assert target.parent.is_dir()
    _ensure_db_dir("sqlite:///:memory:")  # no-op, no crash
    _ensure_db_dir("postgresql://x")  # non-sqlite -> no-op


async def test_check_feed_stale_trips_the_kill_when_holding(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Holding a position + a dead feed = flying blind on risk -> the LATCHING kill.
    worker, _venue, oms, _hb = _worker(tmp_path, _ticks(["100"]), strategy=_AlwaysBuy())
    worker._feed_stale_seconds = 5
    oms._positions[(Venue.BINANCE, SYMBOL)] = _pos(SYMBOL, "0.01")  # an open position
    worker._check_feed_stale()  # no tick yet -> no trip (early return)
    assert worker._risk.is_halted is False
    worker._last_tick_at = NOW - timedelta(seconds=30)  # last tick 30s ago > 5s budget
    worker._check_feed_stale()
    assert worker._risk.is_halted and worker._risk.halt_trigger is KillTrigger.FEED_STALE


async def test_feed_stale_is_soft_when_flat(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # The idle paper soak: a flaky feed stalls while FLAT -> no latch, auto-recovers
    # (the market loop just waits for the next tick). This is the soak-stability fix.
    worker, _venue, _oms, _hb = _worker(tmp_path, _ticks(["100"]), strategy=_AlwaysBuy())
    worker._feed_stale_seconds = 5
    worker._last_tick_at = NOW - timedelta(seconds=300)  # long outage, but flat
    worker._check_feed_stale()
    assert worker._risk.is_halted is False  # nothing held -> nothing to protect


async def test_feed_stale_is_soft_with_only_a_closed_out_position(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A fully closed-out position is a zero-qty row (average_price=None) -> still FLAT,
    # so the `quantity != 0` predicate must read it as flat and NOT latch a stale feed.
    worker, _venue, oms, _hb = _worker(tmp_path, _ticks(["100"]), strategy=_AlwaysBuy())
    worker._feed_stale_seconds = 5
    oms._positions[(Venue.BINANCE, SYMBOL)] = Position(
        venue=Venue.BINANCE,
        symbol=SYMBOL,
        asset_class=AssetClass.CRYPTO,
        quantity=Decimal("0"),
        average_price=None,  # validator: average_price is set iff quantity != 0
        last_price=Decimal("100"),
        updated_at=NOW,
    )
    worker._last_tick_at = NOW - timedelta(seconds=300)
    worker._check_feed_stale()
    assert worker._risk.is_halted is False  # a closed-out residual is flat


async def test_rearm_clears_a_latched_halt_on_clean_reconcile(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A latched halt + a flat book matching broker truth -> re-armed, and the clear is
    # PERSISTED (a fresh restore from the DB no longer sees the halt).
    worker, _venue, oms, _hb = _worker(tmp_path, _ticks(["100"]), strategy=_AlwaysBuy())
    worker._risk.trip(KillTrigger.FEED_STALE)
    await oms.persist_halt(KillTrigger.FEED_STALE)  # durable latch (survives restart)
    rearmed, detail = await worker.rearm()
    assert rearmed and "re-armed" in detail and worker._risk.is_halted is False
    oms.restore_daily_state(worker._today())  # reload from the DB
    assert worker._risk.is_halted is False  # the clear was persisted


async def test_rearm_refuses_when_local_diverges_from_broker(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Open a real position (booked from a fill), then a flat broker -> drift -> the
    # re-arm is REFUSED and the halt is retained (never cleared blind).
    worker, _venue, oms, _hb = _worker(
        tmp_path, _ticks(["100", "100"]), strategy=_AlwaysBuy("0.01")
    )
    await worker.run()  # 2 ticks -> 1 closed bar -> 1 buy -> fill booked locally
    assert oms.positions and oms.positions[0].quantity == Decimal("0.01")
    worker._risk.trip(KillTrigger.MANUAL)
    await oms.persist_halt(KillTrigger.MANUAL)
    rearmed, detail = await worker.rearm()  # broker (fake) reports no position -> drift
    assert rearmed is False and "not clean" in detail
    assert worker._risk.is_halted is True


async def test_rearm_is_a_noop_when_not_halted(tmp_path) -> None:  # type: ignore[no-untyped-def]
    worker, *_ = _worker(tmp_path, _ticks(["100"]), strategy=_AlwaysBuy())
    rearmed, detail = await worker.rearm()
    assert rearmed and "nothing to re-arm" in detail


async def test_status_detail_reports_run_state_and_str_money(tmp_path) -> None:  # type: ignore[no-untyped-def]
    worker, *_ = _worker(tmp_path, _ticks(["100"]), strategy=_AlwaysBuy())
    worker._last_tick_at = NOW
    d = worker._status_detail()
    assert d["run_state"] == "running"
    assert d["strategy"] == "idle"
    assert isinstance(d["realized_pnl"], str)  # money as str, never float (B5)
    assert isinstance(d["unrealized_pnl"], str)
    assert d["open_positions"] == 0
    assert d["last_tick_age_s"] is not None


async def test_pod_status_loop_beats_then_stops(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    rec = _RecordingPodStatus()
    worker, *_ = _worker(
        tmp_path, _ticks(["100"]), strategy=_AlwaysBuy(), pod_status=cast(PodStatusWriter, rec)
    )
    worker._last_tick_at = NOW

    async def _sleep_then_stop(_seconds: float) -> None:
        worker._control.set(RunState.STOPPED)  # exit the loop after one beat

    monkeypatch.setattr("worker.loop.asyncio.sleep", _sleep_then_stop)
    await worker._pod_status_loop()
    assert len(rec.beats) == 1
    assert rec.beats[0]["armed"] is True  # not halted
    assert rec.beats[0]["detail"]["strategy"] == "idle"


async def test_pod_status_loop_self_heals_on_error(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A telemetry error must never kill the heartbeat loop (best-effort; off the money path).
    class _Boom:
        async def beat(self, **_kw: Any) -> None:
            raise RuntimeError("telemetry boom")

    worker, *_ = _worker(
        tmp_path, _ticks(["100"]), strategy=_AlwaysBuy(), pod_status=cast(PodStatusWriter, _Boom())
    )
    worker._last_tick_at = NOW

    async def _sleep_then_stop(_seconds: float) -> None:
        worker._control.set(RunState.STOPPED)

    monkeypatch.setattr("worker.loop.asyncio.sleep", _sleep_then_stop)
    await worker._pod_status_loop()  # must NOT raise — the error is logged and the loop continues


async def test_run_wires_the_pod_status_heartbeat(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # run() starts the pod-status background task; a bounded feed lets it beat at least
    # once (at the first market-loop await) before the loop ends and cancels it.
    rec = _RecordingPodStatus()
    worker, *_ = _worker(
        tmp_path,
        _ticks(["100", "100", "100"]),
        strategy=_AlwaysBuy(),
        pod_status=cast(PodStatusWriter, rec),
    )
    await worker.run()
    assert rec.beats  # the heartbeat fired


async def test_periodic_reconciles_once(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Drive exactly one periodic cycle (stub sleep to stop the loop after it). The
    # periodic does NOT beat the heartbeat (that is market-loop-only, TEST-5).
    worker, _venue, _oms, _hb = _worker(tmp_path, _ticks(["100"]), strategy=_AlwaysBuy())
    worker._last_tick_at = NOW  # fresh -> no feed-stale trip

    async def _sleep_then_stop(_seconds: float) -> None:
        worker._control.set(RunState.STOPPED)  # exit the while-loop after this iteration

    monkeypatch.setattr("worker.loop.asyncio.sleep", _sleep_then_stop)
    await worker._periodic()
    assert worker._risk.is_halted is False  # clean reconcile against a flat book


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
        # The real adapter's order_events() is continuous -> background consume_events.
        assert worker._drain_inline is False
    finally:
        await worker._adapter.aclose()


async def test_build_worker_non_streaming_uses_consume(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # REGRESSION: a non-streaming (REST-poll) venue's order_events() is an INFINITE poll,
    # so the worker must consume it via the background consume_events task — NOT the inline
    # drain_events, which would `async for` the infinite poll forever and wedge the market
    # loop on the first closed bar (heartbeat freezes -> deadman trips).
    from worker.loop import build_worker

    monkeypatch.setenv("DELTA_TESTNET_API_KEY", "k")
    monkeypatch.setenv("DELTA_TESTNET_API_SECRET", "s")
    env = EnvConfig.model_validate(
        {
            "env": "paper",
            "mode": "paper",
            "allow_live": False,
            "worker_id": "w",
            "venue": "delta-testnet",  # streaming=False (REST poll)
            "strategy": "idle",
            "symbols": ["BTC/USD:USD"],
            "bar_interval_seconds": 60,
            "state_db": "sqlite:///:memory:",
            "heartbeat_path": f"{tmp_path}/hb",
            "command_poll_seconds": 1.0,
            "reconcile_interval_seconds": 30,
        }
    )
    worker = build_worker(env)
    try:
        assert worker._drain_inline is False  # background consume_events, never inline drain
    finally:
        await worker._adapter.aclose()


async def test_build_worker_wires_pod_sync_when_a_pod_is_present(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # With a pod client present, build_worker wires BOTH the command bus (pod -> worker)
    # and the status heartbeat (worker -> pod); absent a pod, both stay None (default path).
    from worker.loop import build_worker

    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "k")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "s")
    monkeypatch.setattr("worker.loop.build_pod_client", lambda env: object())  # a non-None pod
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
            "pod_sync": {"pod_id": "p-1"},
        }
    )
    worker = build_worker(env)
    try:
        assert worker._command_watcher is not None
        assert worker._pod_status is not None
    finally:
        await worker._adapter.aclose()


async def test_token_refresh_swaps_clients_on_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pod token is ~60-min + the worker reads it only at startup, so the rotation loop re-reads
    # $token_env from the .env FILE and swaps a freshly-tokened client into BOTH the status writer
    # and the command source — no restart. (A Mac relay keeps the file fresh; here the test does.)
    from worker.loop import build_worker

    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "k")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "s")
    monkeypatch.setenv("LEMMA_TOKEN", "old.token")  # the startup token the clients were built with
    built: list[tuple[str | None, object]] = []

    def _fake_build(env: object, *, token: str | None = None) -> object:
        pod = object()
        built.append((token, pod))
        return pod

    monkeypatch.setattr("worker.loop.build_pod_client", _fake_build)
    envfile = tmp_path / ".env"
    envfile.write_text("LEMMA_TOKEN=old.token\n", encoding="utf-8")
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
            "pod_sync": {
                "pod_id": "p-1",
                "token_envfile": str(envfile),
                "token_refresh_seconds": 0.01,
            },
        }
    )
    worker = build_worker(env)
    try:
        envfile.write_text(
            "LEMMA_TOKEN=new.token\n", encoding="utf-8"
        )  # the relay rotates the file
        task = asyncio.create_task(worker._pod_token_refresh_loop())
        for _ in range(100):  # wait (bounded) for the loop to pick up the change
            await asyncio.sleep(0.01)
            if any(tok == "new.token" for tok, _ in built):
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        fresh = [pod for tok, pod in built if tok == "new.token"]
        assert fresh, "the loop never rebuilt the client with the fresh token"
        assert worker._pod_status is not None
        assert worker._pod_status._pod is fresh[0]  # status writer now uses the fresh client
        assert worker._pod_command_source is not None
        assert worker._pod_command_source._pod is fresh[0]  # command source swapped too
    finally:
        await worker._adapter.aclose()


async def test_token_refresh_no_swap_when_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If the file token matches the in-use token, the loop must NOT rebuild (no churn/log spam).
    from worker.loop import build_worker

    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "k")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "s")
    monkeypatch.setenv("LEMMA_TOKEN", "same.token")
    builds: list[str | None] = []

    def _track_build(env: object, *, token: str | None = None) -> object:
        builds.append(token)
        return object()

    monkeypatch.setattr("worker.loop.build_pod_client", _track_build)
    reads = {"n": 0}

    def _counting_read(path: str, key: str) -> str:
        reads["n"] += 1
        return "same.token"  # what the file holds — unchanged vs the startup token

    monkeypatch.setattr("worker.loop.read_envfile_token", _counting_read)
    envfile = tmp_path / ".env"
    envfile.write_text("LEMMA_TOKEN=same.token\n", encoding="utf-8")  # unchanged vs startup
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
            "pod_sync": {
                "pod_id": "p-1",
                "token_envfile": str(envfile),
                "token_refresh_seconds": 0.01,
            },
        }
    )
    worker = build_worker(env)
    builds.clear()  # ignore the build_worker startup call; watch only the loop
    try:
        task = asyncio.create_task(worker._pod_token_refresh_loop())
        await asyncio.sleep(0.05)  # several iterations
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert reads["n"] >= 1  # the loop DID iterate + read the file...
        assert builds == []  # ...and correctly did not rebuild on an unchanged token
    finally:
        await worker._adapter.aclose()


async def test_kill_event_mirrored_to_risk_events_on_a_new_trip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A new kill-switch trip is mirrored to the pod risk_events log (mission control) from the
    # telemetry loop, off the money path — one row per distinct trip, never a duplicate.
    rec = _RecordingPodStatus()
    worker, *_ = _worker(
        tmp_path, _ticks(["100"]), strategy=_AlwaysBuy(), pod_status=cast(PodStatusWriter, rec)
    )
    await worker._maybe_sync_kill_event()  # not halted -> nothing
    assert rec.risk_events == []
    worker._risk.trip(KillTrigger.DAILY_LOSS)  # a kill trips
    await worker._maybe_sync_kill_event()
    assert len(rec.risk_events) == 1
    ev = rec.risk_events[0]
    assert ev["kind"] == "kill_tripped" and ev["severity"] == "critical"
    assert ev["detail"]["trigger"] == "daily_loss"
    assert ev["detail"]["worker_id"] == "w-test"
    await worker._maybe_sync_kill_event()  # same trip, no new generation
    assert len(rec.risk_events) == 1  # not re-reported


async def test_kill_event_not_remirrored_for_a_baselined_halt(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A worker that boots already-halted (the latched halt restored) baselines the cursor at the
    # current generation, so the old, already-alerted kill is NOT re-reported to risk_events.
    rec = _RecordingPodStatus()
    worker, *_ = _worker(
        tmp_path, _ticks(["100"]), strategy=_AlwaysBuy(), pod_status=cast(PodStatusWriter, rec)
    )
    worker._risk.trip(KillTrigger.MANUAL)  # already halted on entry
    worker._synced_halt_generation = worker._risk.halt_generation  # the loop baselines to current
    await worker._maybe_sync_kill_event()
    assert rec.risk_events == []  # a known (baselined) halt is not re-mirrored
