"""Command-bus tests — pod → worker commands (TEST-8, M3.6).

Drives the watcher through a *real* OMS + PaperBroker + Reconciler so the
flatten / kill / clear-halt paths actually move the book, with an in-memory
CommandSource standing in for the pod `commands` table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from alpha_core.adapters.paper import PaperBroker
from alpha_core.core.enums import AssetClass, OrderType, Side, Venue
from alpha_core.core.models import Position, Signal, Tick
from alpha_core.execution.commands import (
    Command,
    CommandKind,
    CommandStatus,
    CommandWatcher,
    RunState,
    WorkerControl,
)
from alpha_core.execution.costs import CostModel, InstrumentMeta
from alpha_core.execution.oms import OMS
from alpha_core.execution.reconcile import Reconciler
from alpha_core.execution.state import StateStore
from alpha_core.risk.limits import RiskConfig
from alpha_core.risk.manager import KillTrigger, RiskManager

T0 = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)
SYMBOL = "BTC/USDT"
WORKER = "worker-1"

COST_CONFIG = {
    "slippage": {
        "crypto": {"type": "bps", "value": 8},
        "default_spread": {"equity": 0.0005, "crypto": 0.0008, "index_option_ticks": 1},
        "stress_multiplier": 2,
    },
    "segments": {"crypto": {"trading_fee": {"pct": 0.001, "side": "both"}}},
}


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
                    "max_orders_per_minute": 20,
                    "max_daily_loss_halt": "0.10",
                    "max_loss_per_trade": "0.05",
                    "per_segment_exposure_cap": "1.00",
                },
            }
        )
    )


def _setup(risk: RiskManager | None = None) -> tuple[OMS, PaperBroker, Reconciler, RiskManager]:
    risk = risk or _risk()
    broker = PaperBroker(
        cost_model=CostModel(COST_CONFIG),
        instruments={SYMBOL: InstrumentMeta(asset_class=AssetClass.CRYPTO)},
        starting_cash=Decimal("1000000"),
    )
    broker.on_tick(
        Tick(
            symbol=SYMBOL,
            venue=Venue.BINANCE,
            asset_class=AssetClass.CRYPTO,
            ts=T0,
            bid=Decimal("99"),
            ask=Decimal("101"),
            last_price=Decimal("100"),
        )
    )
    store = StateStore("sqlite:///:memory:")
    store.create_schema()
    oms = OMS(adapter=broker, risk=risk, store=store, venue=Venue.BINANCE)
    reconciler = Reconciler(adapter=broker, risk=risk)
    return oms, broker, reconciler, risk


async def _open_position(oms: OMS, qty: str = "1") -> None:
    """Establish a long position via a market buy so there is something to flatten."""
    sig = Signal(
        strategy_id="s1",
        symbol=SYMBOL,
        asset_class=AssetClass.CRYPTO,
        side=Side.BUY,
        quantity=Decimal(qty),
        order_type=OrderType.MARKET,
        created_at=T0,
    )
    await oms.submit_signal(sig, reference_price=Decimal("101"))
    await oms.drain_events()


class _FakeSource:
    """In-memory command bus: poll returns PENDING commands; ack updates status."""

    def __init__(self, commands: list[Command]) -> None:
        self._commands = commands
        self.status: dict[str, CommandStatus] = {c.id: CommandStatus.PENDING for c in commands}
        self.acks: list[tuple[str, CommandStatus, str | None]] = []

    async def poll(self) -> list[Command]:
        return [c for c in self._commands if self.status[c.id] is CommandStatus.PENDING]

    async def ack(
        self, command_id: str, status: CommandStatus, *, detail: str | None = None
    ) -> None:
        self.status[command_id] = status
        self.acks.append((command_id, status, detail))


def _cmd(kind: CommandKind, *, cid: str = "c1", worker_id: str | None = None) -> Command:
    return Command(id=cid, kind=kind, worker_id=worker_id)


def _watcher(
    source: _FakeSource,
    oms: OMS,
    risk: RiskManager,
    *,
    control: WorkerControl | None = None,
    reconciler: Reconciler | None = None,
    token_refresh: object | None = None,
) -> CommandWatcher:
    return CommandWatcher(
        source=source,
        oms=oms,
        risk=risk,
        control=control or WorkerControl(),
        worker_id=WORKER,
        reconciler=reconciler,
        token_refresh=token_refresh,  # type: ignore[arg-type]
        drain=True,  # PaperBroker fills synchronously — pull them inline
    )


# --- the trip path (TEST-8) -----------------------------------------------------


async def test_emergency_flatten_trips_and_flattens() -> None:
    oms, _broker, _rec, risk = _setup()
    await _open_position(oms)
    assert oms.positions[0].quantity == Decimal("1")
    source = _FakeSource([_cmd(CommandKind.EMERGENCY_FLATTEN)])
    results = await _watcher(source, oms, risk).process_once()
    assert results == [(source._commands[0], CommandStatus.DONE)]
    assert all(p.quantity == 0 for p in oms.positions)  # flattened
    assert risk.is_halted is True  # latched (the trip path)
    assert source.status["c1"] is CommandStatus.DONE


async def test_arm_kill_latches_and_pauses() -> None:
    oms, _broker, _rec, risk = _setup()
    await _open_position(oms)
    control = WorkerControl()
    source = _FakeSource([_cmd(CommandKind.ARM_KILL)])
    await _watcher(source, oms, risk, control=control).process_once()
    assert risk.is_halted is True
    assert control.state is RunState.PAUSED
    assert all(p.quantity == 0 for p in oms.positions)


async def test_soft_flatten_does_not_latch_but_pauses() -> None:
    oms, _broker, _rec, risk = _setup()
    await _open_position(oms)
    control = WorkerControl()
    source = _FakeSource([_cmd(CommandKind.FLATTEN)])
    await _watcher(source, oms, risk, control=control).process_once()
    assert all(p.quantity == 0 for p in oms.positions)  # flattened
    assert risk.is_halted is False  # but NOT latched — reversible
    assert control.state is RunState.PAUSED


# --- clear_halt: only on a clean reconcile -------------------------------------


async def test_clear_halt_rearms_on_clean_reconcile() -> None:
    oms, _broker, rec, risk = _setup()
    risk.trip(KillTrigger.MANUAL)  # already halted, flat book
    control = WorkerControl(RunState.PAUSED)
    source = _FakeSource([_cmd(CommandKind.CLEAR_HALT)])
    await _watcher(source, oms, risk, control=control, reconciler=rec).process_once()
    assert risk.is_halted is False  # re-armed
    assert control.state is RunState.RUNNING


async def test_clear_halt_refuses_on_dirty_reconcile() -> None:
    oms, _broker, rec, risk = _setup()
    risk.trip(KillTrigger.MANUAL)
    # Local thinks it holds a position the broker (flat) does not -> unexplained drift.
    oms._positions[(Venue.BINANCE, SYMBOL)] = Position(
        venue=Venue.BINANCE,
        symbol=SYMBOL,
        asset_class=AssetClass.CRYPTO,
        quantity=Decimal("5"),
        average_price=Decimal("100"),
        last_price=Decimal("100"),
        updated_at=T0,
    )
    source = _FakeSource([_cmd(CommandKind.CLEAR_HALT)])
    await _watcher(source, oms, risk, reconciler=rec).process_once()
    assert risk.is_halted is True  # halt retained — never clear blind
    assert "not clean" in (source.acks[-1][2] or "")


async def test_clear_halt_without_reconciler_refuses() -> None:
    oms, _broker, _rec, risk = _setup()
    risk.trip(KillTrigger.MANUAL)
    source = _FakeSource([_cmd(CommandKind.CLEAR_HALT)])
    await _watcher(source, oms, risk, reconciler=None).process_once()
    assert risk.is_halted is True
    assert "no reconciler" in (source.acks[-1][2] or "")


# --- run-state control ----------------------------------------------------------


async def test_pause_stop_start_control() -> None:
    oms, _broker, _rec, risk = _setup()
    control = WorkerControl()
    for kind, expected in [
        (CommandKind.PAUSE, RunState.PAUSED),
        (CommandKind.START, RunState.RUNNING),
        (CommandKind.STOP, RunState.STOPPED),
    ]:
        source = _FakeSource([_cmd(kind, cid=kind.value)])
        await _watcher(source, oms, risk, control=control).process_once()
        assert control.state is expected


async def test_start_refused_while_halted() -> None:
    oms, _broker, _rec, risk = _setup()
    risk.trip(KillTrigger.MANUAL)
    control = WorkerControl(RunState.PAUSED)
    source = _FakeSource([_cmd(CommandKind.START)])
    await _watcher(source, oms, risk, control=control).process_once()
    assert control.state is RunState.PAUSED  # not resumed
    assert "refused" in (source.acks[-1][2] or "")


# --- routing / acking / resilience ---------------------------------------------


async def test_command_for_other_worker_is_skipped() -> None:
    oms, _broker, _rec, risk = _setup()
    source = _FakeSource([_cmd(CommandKind.PAUSE, worker_id="worker-2")])
    control = WorkerControl()
    results = await _watcher(source, oms, risk, control=control).process_once()
    assert results == []  # not ours
    assert source.status["c1"] is CommandStatus.PENDING  # left for the right worker
    assert control.state is RunState.RUNNING


async def test_global_command_applies() -> None:
    oms, _broker, _rec, risk = _setup()
    control = WorkerControl()
    source = _FakeSource([_cmd(CommandKind.PAUSE, worker_id=None)])  # global
    await _watcher(source, oms, risk, control=control).process_once()
    assert control.state is RunState.PAUSED


async def test_refresh_token_noop_without_callback() -> None:
    oms, _broker, _rec, risk = _setup()
    source = _FakeSource([_cmd(CommandKind.REFRESH_TOKEN)])
    await _watcher(source, oms, risk).process_once()
    assert source.status["c1"] is CommandStatus.DONE
    assert "n/a" in (source.acks[-1][2] or "")


async def test_refresh_token_invokes_callback() -> None:
    oms, _broker, _rec, risk = _setup()
    called: list[dict[str, object] | None] = []

    async def _refresh(payload: dict[str, object] | None) -> None:
        called.append(payload)

    source = _FakeSource([Command(id="c1", kind=CommandKind.REFRESH_TOKEN, payload={"t": "abc"})])
    await _watcher(source, oms, risk, token_refresh=_refresh).process_once()
    assert called == [{"t": "abc"}]
    assert source.status["c1"] is CommandStatus.DONE


def test_worker_control_properties() -> None:
    c = WorkerControl()
    assert c.should_submit is True and c.stopped is False
    c.set(RunState.PAUSED)
    assert c.should_submit is False and c.stopped is False
    c.set(RunState.STOPPED)
    assert c.should_submit is False and c.stopped is True


async def test_soft_flatten_with_drain_false_skips_inline_drain() -> None:
    oms, _broker, _rec, risk = _setup()
    await _open_position(oms)
    control = WorkerControl()
    source = _FakeSource([_cmd(CommandKind.FLATTEN)])
    watcher = CommandWatcher(  # drain=False: the live event model (no inline drain)
        source=source, oms=oms, risk=risk, control=control, worker_id=WORKER, drain=False
    )
    await watcher.process_once()
    assert control.state is RunState.PAUSED
    assert risk.is_halted is False


async def test_run_processes_then_stops(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def _nosleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("alpha_core.execution.commands.asyncio.sleep", _nosleep)
    oms, _broker, _rec, risk = _setup()
    control = WorkerControl()
    source = _FakeSource([_cmd(CommandKind.STOP)])
    await _watcher(source, oms, risk, control=control).run(poll_interval=0.01)
    assert control.stopped is True  # the loop processed STOP and exited


async def test_run_survives_a_poll_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def _nosleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("alpha_core.execution.commands.asyncio.sleep", _nosleep)
    oms, _broker, _rec, risk = _setup()
    control = WorkerControl()

    class _FlakySource(_FakeSource):
        def __init__(self, commands: list[Command]) -> None:
            super().__init__(commands)
            self._first = True

        async def poll(self) -> list[Command]:
            if self._first:
                self._first = False
                raise RuntimeError("pod unreachable")  # one transient failure
            return await super().poll()

    source = _FlakySource([_cmd(CommandKind.STOP)])
    await _watcher(source, oms, risk, control=control).run(poll_interval=0.01)
    assert control.stopped is True  # survived the error, then processed STOP


async def test_failing_command_is_acked_failed_and_bus_continues() -> None:
    oms, _broker, _rec, risk = _setup()

    async def _boom(payload: dict[str, object] | None) -> None:
        raise RuntimeError("token endpoint down")

    source = _FakeSource([_cmd(CommandKind.REFRESH_TOKEN)])
    results = await _watcher(source, oms, risk, token_refresh=_boom).process_once()
    assert results[0][1] is CommandStatus.FAILED
    assert source.status["c1"] is CommandStatus.FAILED
    assert "token endpoint down" in (source.acks[-1][2] or "")
