"""Deadman tests — the independent flatten-on-dead-worker watchdog (TEST-5, R1)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from alpha_core.core.enums import AssetClass, OrderState, OrderType, Side, Venue
from alpha_core.core.interfaces import BrokerAdapter, BrokerOrderEvent
from alpha_core.core.models import Order, Position, Tick
from alpha_core.execution.deadman import (
    Deadman,
    DeadmanConfig,
    HeartbeatFile,
    LivenessSource,
    load_deadman_config,
)
from alpha_core.observability.notify import Notifier, Severity

T0 = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)


def _cfg(rto: float = 15.0, poll: float = 2.0) -> DeadmanConfig:
    return DeadmanConfig(rto_seconds=rto, poll_interval_seconds=poll)


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


def _order(cid: str, symbol: str, state: OrderState) -> Order:
    return Order(
        client_order_id=cid,
        symbol=symbol,
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        limit_price=Decimal("90"),
        state=state,
        strategy_id="s1",
        created_at=T0,
        updated_at=T0,
    )


class _FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class _FakeLiveness:
    """Controllable LivenessSource: set last_beat / emergency directly."""

    def __init__(self, *, beat: datetime | None = None, emergency: bool = False) -> None:
        self._beat = beat
        self._emergency = emergency

    def last_beat(self) -> datetime | None:
        return self._beat

    def flatten_requested(self) -> bool:
        return self._emergency


class _FakeAdapter(BrokerAdapter):
    """Tracks placed flatten orders + cancellations; flattens fill on the next read
    unless ``auto_fill`` is off (to exercise the in-flight idempotency window)."""

    def __init__(
        self,
        positions: list[Position],
        orders: list[Order] | None = None,
        *,
        auto_fill: bool = True,
    ) -> None:
        self._positions = positions
        self._orders = orders or []
        self._auto_fill = auto_fill
        self.placed: list[Order] = []
        self.cancelled: list[str] = []

    async def place_order(self, order: Order) -> str:
        self.placed.append(order)
        if self._auto_fill:  # simulate the market flatten executing -> position goes flat
            self._positions = [p for p in self._positions if p.symbol != order.symbol]
        return f"V-{order.client_order_id}"

    async def cancel(self, client_order_id: str) -> None:
        self.cancelled.append(client_order_id)

    async def modify(self, client_order_id: str, **kwargs: object) -> None:
        raise NotImplementedError

    async def get_positions(self) -> list[Position]:
        return list(self._positions)

    async def get_orders(self) -> list[Order]:
        return list(self._orders)

    async def find_order_id(self, client_order_id: str) -> str | None:
        return None

    def subscribe_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        raise NotImplementedError

    def order_events(self) -> AsyncIterator[BrokerOrderEvent]:
        raise NotImplementedError


class _RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[tuple[str, Severity]] = []

    def send(self, message: str, *, severity: Severity = Severity.INFO) -> None:
        self.sent.append((message, severity))


def _deadman(
    adapter: _FakeAdapter,
    liveness: LivenessSource,
    clock: _FakeClock,
    *,
    notifier: Notifier | None = None,
    cfg: DeadmanConfig | None = None,
) -> Deadman:
    return Deadman(
        adapter=adapter,
        liveness=liveness,
        config=cfg or _cfg(),
        venue=Venue.BINANCE,
        clock=clock,
        notifier=notifier,
    )


# --- config --------------------------------------------------------------------


def test_config_rejects_poll_slower_than_rto() -> None:
    with pytest.raises(ValidationError):
        DeadmanConfig(rto_seconds=5, poll_interval_seconds=10)


def test_loads_from_worker_yaml() -> None:
    cfg = load_deadman_config()  # config/worker.yaml ships valid budgets
    assert cfg.enabled is True
    assert cfg.rto_seconds > 0
    assert cfg.poll_interval_seconds <= cfg.rto_seconds


# --- liveness / heartbeat file -------------------------------------------------


def test_heartbeat_file_roundtrips_utc(tmp_path) -> None:  # type: ignore[no-untyped-def]
    hb = HeartbeatFile(tmp_path / "beat" / "hb.txt")  # also creates the parent dir
    assert hb.last_beat() is None  # nothing written yet
    hb.beat(T0)
    got = hb.last_beat()
    assert got == T0 and got.tzinfo is not None
    assert hb.flatten_requested() is False


def test_heartbeat_file_tolerates_garbage(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "hb.txt"
    path.write_text("not-a-timestamp")
    assert HeartbeatFile(path).last_beat() is None


# --- healthy worker: no action -------------------------------------------------


async def test_fresh_heartbeat_does_not_trip() -> None:
    clock = _FakeClock(T0)
    adapter = _FakeAdapter([_pos("BTC/USDT", "1")])
    dm = _deadman(adapter, _FakeLiveness(beat=T0), clock)
    report = await dm.check()
    assert report.tripped is False
    assert adapter.placed == []  # the open position is left alone


async def test_grace_before_first_beat_then_trips() -> None:
    # last_beat is None (worker hasn't beaten). Within one RTO of deadman start it
    # tolerates the gap; past RTO it declares the worker never came up and flattens.
    clock = _FakeClock(T0)
    adapter = _FakeAdapter([_pos("BTC/USDT", "1")])
    dm = _deadman(adapter, _FakeLiveness(beat=None), clock)
    assert (await dm.check()).tripped is False  # within grace
    clock.advance(16)  # past rto=15
    report = await dm.check()
    assert report.tripped is True
    assert "no worker heartbeat" in (report.reason or "")


# --- stale worker: flatten -----------------------------------------------------


async def test_stale_heartbeat_flattens_long_and_short() -> None:
    clock = _FakeClock(T0)
    adapter = _FakeAdapter([_pos("BTC/USDT", "2"), _pos("ETH/USDT", "-3")])
    notifier = _RecordingNotifier()
    dm = _deadman(adapter, _FakeLiveness(beat=T0 - timedelta(seconds=30)), clock, notifier=notifier)
    report = await dm.check()
    assert report.tripped is True and report.first_trip is True
    # opposing market orders: SELL the long, BUY back the short
    by_symbol = {o.symbol: o for o in adapter.placed}
    assert by_symbol["BTC/USDT"].side is Side.SELL and by_symbol["BTC/USDT"].quantity == Decimal(
        "2"
    )
    assert by_symbol["ETH/USDT"].side is Side.BUY and by_symbol["ETH/USDT"].quantity == Decimal("3")
    assert all(o.order_type is OrderType.MARKET for o in adapter.placed)
    assert any(sev is Severity.CRITICAL for _, sev in notifier.sent)  # alerted on trip


async def test_emergency_flatten_trips_even_when_heartbeat_fresh() -> None:
    clock = _FakeClock(T0)
    adapter = _FakeAdapter([_pos("BTC/USDT", "1")])
    dm = _deadman(adapter, _FakeLiveness(beat=T0, emergency=True), clock)
    report = await dm.check()
    assert report.tripped is True
    assert "emergency_flatten" in (report.reason or "")
    assert len(adapter.placed) == 1


async def test_cancels_working_orders_on_trip() -> None:
    clock = _FakeClock(T0)
    adapter = _FakeAdapter(
        [_pos("BTC/USDT", "1")],
        orders=[
            _order("resting1", "BTC/USDT", OrderState.OPEN),
            _order("done", "ETH/USDT", OrderState.FILLED),
        ],
    )
    dm = _deadman(adapter, _FakeLiveness(beat=T0 - timedelta(seconds=30)), clock)
    report = await dm.check()
    assert report.cancelled == ["resting1"]  # the terminal order is not cancelled


# --- idempotency ---------------------------------------------------------------


async def test_flatten_id_stable_across_polls_within_episode() -> None:
    # auto_fill off: the position persists, so two polls both try to flatten it.
    # Within one trip episode the client id must be identical (venue dedups it).
    clock = _FakeClock(T0)
    adapter = _FakeAdapter([_pos("BTC/USDT", "1")], auto_fill=False)
    dm = _deadman(adapter, _FakeLiveness(beat=T0 - timedelta(seconds=30)), clock)
    await dm.check()
    clock.advance(2)
    await dm.check()
    assert len(adapter.placed) == 2
    assert adapter.placed[0].client_order_id == adapter.placed[1].client_order_id


async def test_new_episode_after_recovery_gets_fresh_flatten_id() -> None:
    clock = _FakeClock(T0)
    adapter = _FakeAdapter([_pos("BTC/USDT", "1")], auto_fill=False)
    liveness = _FakeLiveness(beat=T0 - timedelta(seconds=30))
    dm = _deadman(adapter, liveness, clock)
    await dm.check()  # episode 1 trip
    id1 = adapter.placed[-1].client_order_id

    liveness._beat = clock.now()  # worker recovers
    recovered = await dm.check()
    assert recovered.tripped is False

    clock.advance(60)
    liveness._beat = clock.now() - timedelta(seconds=30)  # dies again -> episode 2
    await dm.check()
    id2 = adapter.placed[-1].client_order_id
    assert id1 != id2  # a fresh episode must not reuse the prior episode's id


async def test_only_first_trip_alerts() -> None:
    clock = _FakeClock(T0)
    adapter = _FakeAdapter([_pos("BTC/USDT", "1")], auto_fill=False)
    notifier = _RecordingNotifier()
    dm = _deadman(adapter, _FakeLiveness(beat=T0 - timedelta(seconds=30)), clock, notifier=notifier)
    await dm.check()
    await dm.check()
    criticals = [m for m, sev in notifier.sent if sev is Severity.CRITICAL]
    assert len(criticals) == 1  # the trip alert fires once, not every poll


async def test_trip_on_empty_book_is_a_clean_noop() -> None:
    # Emergency requested but nothing to do: still "tripped", but no orders.
    clock = _FakeClock(T0)
    adapter = _FakeAdapter([])
    dm = _deadman(adapter, _FakeLiveness(beat=T0, emergency=True), clock)
    report = await dm.check()
    assert report.tripped is True
    assert report.cancelled == [] and report.flattened == []


async def test_zero_quantity_position_is_skipped() -> None:
    clock = _FakeClock(T0)
    flat = Position(  # a flat row must carry no average_price (model invariant)
        venue=Venue.BINANCE,
        symbol="XRP/USDT",
        asset_class=AssetClass.CRYPTO,
        quantity=Decimal("0"),
        updated_at=T0,
    )
    adapter = _FakeAdapter([_pos("BTC/USDT", "1"), flat])
    dm = _deadman(adapter, _FakeLiveness(beat=T0 - timedelta(seconds=30)), clock)
    report = await dm.check()
    assert [o.symbol for o in report.flattened] == ["BTC/USDT"]  # the flat one is not traded


async def test_run_swallows_a_check_error_and_keeps_polling(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The deadman is the last line of defence — a broker error in one poll must be
    # logged and survived, not crash the loop. Stub sleep to break out after one pass.
    clock = _FakeClock(T0)

    class _Boom(_FakeAdapter):
        async def get_positions(self) -> list[Position]:
            raise RuntimeError("broker down")

    adapter = _Boom([_pos("BTC/USDT", "1")])
    dm = _deadman(adapter, _FakeLiveness(beat=T0 - timedelta(seconds=30)), clock)

    sleeps = 0

    async def _stub_sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        raise asyncio.CancelledError  # stop after the first poll

    monkeypatch.setattr("alpha_core.execution.deadman.asyncio.sleep", _stub_sleep)
    with pytest.raises(asyncio.CancelledError):
        await dm.run()
    assert sleeps == 1  # the error was swallowed and the loop reached its sleep
