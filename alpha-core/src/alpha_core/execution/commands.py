"""Pod → worker command bus (TEST-8, M3.6).

The pod issues *commands* (rows in the `commands` table); **only the worker
executes them** — the pod never touches the broker (TEST-8). This watcher
consumes an injected ``CommandSource`` (the worker backs it with the pod table
via watchChanges + a ~1s poll fallback, R10) and dispatches each command to the
local risk gate / OMS:

- ``arm_kill`` / ``emergency_flatten`` — the **trip path**: latch the kill-switch
  and flatten the book (cancel working orders → opposing market orders). Trading
  stays halted until a ``clear_halt`` on a *clean* reconcile.
- ``flatten`` — a soft de-risk: cancel + flatten + pause, **without** latching the
  kill-switch (reversible with ``start``).
- ``clear_halt`` — re-arm only after a reconcile comes back CLEAN (broker = truth);
  a dirty book re-trips and the halt is retained (never clear blind).
- ``stop`` / ``pause`` / ``start`` — run-state control (``start`` refuses while halted).
- ``refresh_token`` — relay a fresh daily broker token (equities only; a no-op for
  a crypto venue that needs none).

Each command is acked through its lifecycle (pending → acked → done/failed). The
watcher is portable and broker/pod-SDK-free — it talks only to the OMS, the risk
manager, and the ``CommandSource`` contract.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from alpha_core.execution.oms import OMS
from alpha_core.execution.reconcile import Reconciler, ReconcileStatus
from alpha_core.execution.session import handle_kill
from alpha_core.observability.logging import get_logger
from alpha_core.observability.notify import LoggingNotifier, Notifier, Severity
from alpha_core.risk.manager import KillTrigger, RiskManager


class CommandKind(StrEnum):
    """Mirrors the pod ``commands.kind`` enum."""

    START = "start"
    STOP = "stop"
    PAUSE = "pause"
    FLATTEN = "flatten"
    ARM_KILL = "arm_kill"
    CLEAR_HALT = "clear_halt"
    EMERGENCY_FLATTEN = "emergency_flatten"
    REFRESH_TOKEN = "refresh_token"


class CommandStatus(StrEnum):
    """Mirrors the pod ``commands.status`` enum."""

    PENDING = "pending"
    ACKED = "acked"
    DONE = "done"
    FAILED = "failed"


class RunState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class Command:
    """A pod command, normalized from the `commands` row."""

    id: str
    kind: CommandKind
    worker_id: str | None = None
    deployment_id: str | None = None
    payload: dict[str, object] | None = None


class CommandSource(Protocol):
    """The pending-command feed + ack sink (the worker backs this with the pod table)."""

    async def poll(self) -> list[Command]:
        """Return the currently-pending commands (newest issuance)."""
        ...

    async def ack(
        self, command_id: str, status: CommandStatus, *, detail: str | None = None
    ) -> None:
        """Move a command to ``status`` (acked → done/failed), recording ``detail``."""
        ...


class WorkerControl:
    """Shared run-state the watcher mutates and the worker loop reads."""

    def __init__(self, state: RunState = RunState.RUNNING) -> None:
        self._state = state

    @property
    def state(self) -> RunState:
        return self._state

    @property
    def should_submit(self) -> bool:
        """True iff the loop may submit new signals (only while RUNNING)."""
        return self._state is RunState.RUNNING

    @property
    def stopped(self) -> bool:
        return self._state is RunState.STOPPED

    def set(self, state: RunState) -> None:
        self._state = state


TokenRefresh = Callable[[dict[str, object] | None], Awaitable[None]]


class CommandWatcher:
    """Consumes pod commands and applies them to the local risk gate / OMS (TEST-8)."""

    def __init__(
        self,
        *,
        source: CommandSource,
        oms: OMS,
        risk: RiskManager,
        control: WorkerControl,
        worker_id: str,
        reconciler: Reconciler | None = None,
        notifier: Notifier | None = None,
        token_refresh: TokenRefresh | None = None,
        drain: bool = False,
    ) -> None:
        self._source = source
        self._oms = oms
        self._risk = risk
        self._control = control
        self._worker_id = worker_id
        self._reconciler = reconciler
        self._notifier = notifier or LoggingNotifier()
        self._token_refresh = token_refresh
        # Live loops pass drain=False — the long-lived consume_events books fills;
        # bounded paper/test loops pass drain=True to pull the flatten fills inline.
        self._drain = drain
        self._log = get_logger("commands")

    def _is_for_us(self, cmd: Command) -> bool:
        """A command targets us if it names this worker or is global (no worker_id)."""
        return cmd.worker_id is None or cmd.worker_id == self._worker_id

    async def process_once(self) -> list[tuple[Command, CommandStatus]]:
        """Poll, then dispatch every pending command meant for this worker."""
        results: list[tuple[Command, CommandStatus]] = []
        seen: set[str] = set()
        for cmd in await self._source.poll():
            if cmd.id in seen or not self._is_for_us(cmd):
                continue  # a command for another worker is left pending for it
            seen.add(cmd.id)
            await self._source.ack(cmd.id, CommandStatus.ACKED)
            try:
                detail = await self._dispatch(cmd)
                await self._source.ack(cmd.id, CommandStatus.DONE, detail=detail)
                results.append((cmd, CommandStatus.DONE))
            except Exception as exc:  # a bad command must not stall the bus
                self._log.error(
                    "command_failed", command_id=cmd.id, kind=cmd.kind.value, error=str(exc)
                )
                await self._source.ack(cmd.id, CommandStatus.FAILED, detail=str(exc))
                results.append((cmd, CommandStatus.FAILED))
        return results

    async def _dispatch(self, cmd: Command) -> str | None:
        self._log.info("command", command_id=cmd.id, kind=cmd.kind.value)
        match cmd.kind:
            case CommandKind.ARM_KILL | CommandKind.EMERGENCY_FLATTEN:
                return await self._hard_kill(cmd.kind)
            case CommandKind.FLATTEN:
                return await self._soft_flatten()
            case CommandKind.CLEAR_HALT:
                return await self._clear_halt()
            case CommandKind.STOP:
                self._control.set(RunState.STOPPED)
                return "stopped"
            case CommandKind.PAUSE:
                self._control.set(RunState.PAUSED)
                return "paused"
            case CommandKind.START:
                if self._risk.is_halted:
                    return "refused: halted — clear_halt first"
                self._control.set(RunState.RUNNING)
                return "running"
            case CommandKind.REFRESH_TOKEN:
                return await self._refresh_token(cmd)

    async def _hard_kill(self, kind: CommandKind) -> str:
        """Latch the kill-switch and flatten — the trip path (TEST-8)."""
        self._notifier.send(
            f"command {kind.value}: arming kill-switch + flattening", severity=Severity.CRITICAL
        )
        self._risk.trip(KillTrigger.MANUAL)
        flattened = await handle_kill(
            self._oms, KillTrigger.MANUAL, notifier=self._notifier, drain=self._drain
        )
        self._control.set(RunState.PAUSED)  # belt-and-braces: the loop also stops submitting
        return f"halted + flattened {len(flattened)} position(s)"

    async def _soft_flatten(self) -> str:
        """Cancel + flatten + pause, **without** latching the kill-switch (reversible)."""
        self._notifier.send("command flatten: squaring off + pausing", severity=Severity.WARNING)
        await self._oms.cancel_all_working()
        flattened = await self._oms.flatten_all()
        if self._drain:
            await self._oms.drain_events()
        self._control.set(RunState.PAUSED)
        return f"flattened {len(flattened)} position(s), paused"

    async def _clear_halt(self) -> str:
        """Re-arm only on a CLEAN reconcile (broker = truth); never clear blind."""
        if self._reconciler is None:
            return "refused: no reconciler — cannot verify broker truth"
        report = await self._reconciler.reconcile(
            local_orders=self._oms.orders, local_positions=self._oms.positions
        )
        if report.status is not ReconcileStatus.CLEAN:
            # reconcile already re-tripped the kill on a mismatch; halt is retained.
            return f"refused: reconcile not clean ({len(report.issues)} issue(s)) — halt retained"
        await self._oms.apply_reconciliation(
            adopted_orders=report.adopted_orders, adopted_positions=report.adopted_positions
        )
        self._risk.rearm()
        self._control.set(RunState.RUNNING)
        self._notifier.send(
            "command clear_halt: re-armed on clean reconcile", severity=Severity.WARNING
        )
        return "re-armed (clean reconcile)"

    async def _refresh_token(self, cmd: Command) -> str:
        """Relay a fresh broker token (equities only); a no-op where none is needed."""
        if self._token_refresh is None:
            return "no token refresh configured (n/a for this venue)"
        await self._token_refresh(cmd.payload)
        return "token refreshed"

    async def run(self, *, poll_interval: float) -> None:
        """Poll the command bus forever; a transient error never kills the loop."""
        self._log.info(
            "command_watcher_start", worker_id=self._worker_id, poll_interval=poll_interval
        )
        while not self._control.stopped:
            try:
                await self.process_once()
            except Exception as exc:  # the bus must survive a transient source error
                self._log.error("command_poll_error", error=str(exc))
            await asyncio.sleep(poll_interval)
