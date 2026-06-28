"""The worker run loop — the sole live executor (TEST-8, M3.1/M3.2/M3.4).

Wires the live data path to the same kernel the backtester runs (backtest ≡ live,
ADR 0001): venue ticks → ``BarBuilder`` → ``StrategyEngine`` → risk gate → ``OMS``
→ venue, with the broker as the source of truth (reconcile) and the kill-switch
checked first on every order. The pod is never on this path — it only issues
``commands`` the watcher applies locally (TEST-8).

Concurrency (one event loop):
- **market loop** — the driver: per tick, beat the heartbeat (liveness for the
  deadman, TEST-5), fold into bars, mark the book (re-checks the daily-loss kill),
  and on a closed bar run the strategy → submit signals (only while RUNNING).
- **events** — books fills from the venue's order-event stream (derived P&L, R11),
  or drains inline for a bounded/paper feed.
- **periodic** — every ``reconcile_interval``: reconcile against broker truth (adopt
  if clean, halt if not), then check the feed-stale kill. It does NOT beat the
  heartbeat — that is market-loop-only (per tick), so a wedged loop trips the deadman.
- **commands** — the pod→worker bus (start/stop/flatten/arm_kill/clear_halt), run
  only when a ``CommandWatcher`` is provided; ``build_worker`` wires it once the
  pod-backed ``CommandSource`` lands (the pod-sync increment).

On any latched halt (daily-loss / feed-stale / reconcile) the loop flattens once per
distinct kill via ``handle_kill`` (cancel → flatten) and pauses; it never silently
resumes — a clean reconcile + ``clear_halt`` re-arms it, and a fresh trip flattens
again. Money is ``Decimal``; time is the injected clock; no broker SDK is imported
here (only the kernel + the factory).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

from alpha_core.core.interfaces import BrokerAdapter, DataFeed
from alpha_core.data.bar_builder import BarBuilder
from alpha_core.data.feed import AdapterFeed
from alpha_core.execution.commands import CommandWatcher, RunState, WorkerControl
from alpha_core.execution.deadman import HeartbeatFile
from alpha_core.execution.oms import OMS
from alpha_core.execution.reconcile import Reconciler, ReconcileStatus
from alpha_core.execution.session import handle_kill, rearm_on_clean_reconcile
from alpha_core.execution.state import StateStore
from alpha_core.observability.logging import get_logger
from alpha_core.observability.notify import LoggingNotifier, Notifier, Severity
from alpha_core.risk.limits import load_risk_config
from alpha_core.risk.manager import KillTrigger, RiskManager
from alpha_core.scheduler.clock import Clock, SystemClock
from alpha_core.strategy.engine import StrategyEngine
from alpha_core.strategy.registry import build_strategy
from worker.adapters import build_adapter
from worker.config import EnvConfig, active_venue, load_env_config, load_venues
from worker.pod_sync import (
    PodCommandSource,
    PodStatusWriter,
    build_pod_client,
    read_envfile_token,
)


class Worker:
    """Owns the live loop. Constructed with its components (injectable for tests)."""

    def __init__(
        self,
        *,
        env: EnvConfig,
        adapter: BrokerAdapter,
        oms: OMS,
        risk: RiskManager,
        reconciler: Reconciler,
        engine: StrategyEngine,
        bar_builder: BarBuilder,
        heartbeat: HeartbeatFile,
        control: WorkerControl,
        feed: DataFeed,
        feed_stale_seconds: float,
        drain_inline: bool = False,
        notifier: Notifier | None = None,
        command_watcher: CommandWatcher | None = None,
        pod_status: PodStatusWriter | None = None,
        pod_command_source: PodCommandSource | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._env = env
        self._adapter = adapter
        self._oms = oms
        self._risk = risk
        self._reconciler = reconciler
        self._engine = engine
        self._bars = bar_builder
        self._heartbeat = heartbeat
        self._control = control
        self._feed = feed
        self._feed_stale_seconds = feed_stale_seconds
        # True ONLY for a BOUNDED order_events() (a PaperBroker sim that returns after
        # yielding pending fills): drain_events async-for's the stream to exhaustion. A
        # continuous adapter (ws stream OR infinite REST poll) MUST be False — use the
        # consume_events background task — or the inline drain never returns and wedges
        # the market loop on the first closed bar.
        self._drain_inline = drain_inline
        self._notifier = notifier or LoggingNotifier()
        self._command_watcher = command_watcher
        self._pod_status = pod_status  # best-effort worker->pod heartbeat (None = off; TEST-8)
        # The status writer + command source whose pod client the token-rotation loop swaps when
        # the relay refreshes $token_env in the .env file (so a long-lived worker stays pod-synced
        # without a restart). Both best-effort (TEST-8) — rotation never touches trading/safety.
        self._pod_command_source = pod_command_source
        self._clock = clock or SystemClock()
        self._log = get_logger("worker")
        self._last_tick_at: datetime | None = None
        # Flatten once per distinct kill (by the risk manager's halt generation), so a
        # re-trip after a clear_halt re-arm is never skipped (review BLOCKER 2).
        self._handled_halt_generation = -1
        # The kill generation last MIRRORED to the pod risk_events log (best-effort telemetry,
        # off the money path — see _pod_status_loop). -1 until the pod-sync loop baselines it.
        self._synced_halt_generation = -1

    def _now(self) -> datetime:
        return self._clock.now()

    def _today(self) -> str:
        return self._now().astimezone(UTC).date().isoformat()

    async def start(self) -> None:
        """Restart-safe startup: rebuild state, restore the latched halt, then the
        mandatory reconcile gate (broker = truth) before any trading (TEST-7)."""
        self._oms.rebuild_state()
        self._oms.restore_daily_state(self._today())
        self._heartbeat.beat(self._now())
        clean = await self._reconciler.startup_gate(
            local_orders=self._oms.orders, local_positions=self._oms.positions
        )
        if not clean:
            self._control.set(RunState.PAUSED)
            self._log.error("startup_reconcile_halt", worker_id=self._env.worker_id)
            self._notifier.send(
                "startup reconcile not clean — worker paused, halt latched",
                severity=Severity.CRITICAL,
            )
        else:
            self._log.info(
                "worker_start",
                worker_id=self._env.worker_id,
                venue=self._env.venue,
                strategy=self._env.strategy,
                symbols=self._env.symbols,
            )

    async def run(self) -> None:
        """Start the background tasks and drive the market loop until stop/feed-end."""
        await self.start()
        background = [asyncio.create_task(self._periodic())]
        if not self._drain_inline:
            background.append(asyncio.create_task(self._consume_events()))
        if self._command_watcher is not None:
            background.append(
                asyncio.create_task(
                    self._command_watcher.run(poll_interval=self._env.command_poll_seconds)
                )
            )
        if self._pod_status is not None:
            background.append(asyncio.create_task(self._pod_status_loop()))
            if self._env.pod_sync is not None and self._env.pod_sync.token_refresh_seconds > 0:
                background.append(asyncio.create_task(self._pod_token_refresh_loop()))
        try:
            await self._market_loop()
        finally:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            await self._shutdown()

    async def _market_loop(self) -> None:
        async for tick in self._feed.stream_ticks(self._env.symbols):
            if self._control.stopped:
                break
            now = self._now()
            self._last_tick_at = now
            self._heartbeat.beat(now)
            bar = self._bars.add(tick)
            if bar is not None:
                # Same order as the backtest runner (parity, ADR 0001): submit -> book
                # fills -> mark -> kill-check, so a daily-loss kill sees the just-booked P&L.
                if self._control.should_submit:
                    for signal in self._engine.process_bar(bar):
                        await self._oms.submit_signal(signal, reference_price=bar.close)
                if self._drain_inline:
                    await self._oms.drain_events()
                self._oms.mark({bar.symbol: bar.close})  # re-checks the daily-loss kill
                await self._maybe_flatten_on_halt()

    async def _consume_events(self) -> None:
        """Long-lived fill consumer (the venue order-event ws stream)."""
        await self._oms.consume_events()

    async def _pod_status_loop(self) -> None:
        """Best-effort ``worker_status`` heartbeat to the pod every ``heartbeat_seconds``.
        The pod is mission control, NOT on the money path (TEST-8): ``beat`` swallows every
        pod error, and this task is independent of the market/periodic/command tasks, so a
        pod outage cannot stall trading, the kill-switch, the deadman, or reconcile."""
        assert self._pod_status is not None  # only started when present
        cadence = self._env.pod_sync.heartbeat_seconds if self._env.pod_sync else 15.0
        # Baseline the kill-event cursor at the CURRENT generation so a restart-while-halted
        # (the latched halt restored on boot) does not re-report an old, already-alerted kill —
        # only kills that trip AFTER the worker is up are mirrored to the pod risk_events log.
        self._synced_halt_generation = self._risk.halt_generation
        while not self._control.stopped:
            try:
                await self._pod_status.beat(
                    now=self._now(),
                    armed=not self._risk.is_halted,
                    positions=self._oms.positions,
                    detail=self._status_detail(),
                )
                await self._maybe_sync_kill_event()
            except Exception as exc:  # self-healing: telemetry must never kill its own loop
                self._log.warning("pod_status_loop_error", error=str(exc))
            await asyncio.sleep(cadence)

    async def _maybe_sync_kill_event(self) -> None:
        """Mirror a NEW kill-switch trip to the pod ``risk_events`` log (mission control). Detected
        HERE, in the telemetry loop (off the money path) — never on the kill path — by the risk
        manager's halt generation, so a pod write can never delay the flatten/kill-switch (TEST-8).
        Best-effort: ``record_risk_event`` swallows pod errors. One row per distinct trip."""
        if self._pod_status is None:
            return
        gen = self._risk.halt_generation
        if self._risk.is_halted and gen != self._synced_halt_generation:
            self._synced_halt_generation = gen  # claim before the await (no duplicate row)
            trigger = self._risk.halt_trigger
            await self._pod_status.record_risk_event(
                kind="kill_tripped",
                severity="critical",
                now=self._now(),
                detail={
                    "worker_id": self._env.worker_id,
                    "venue": self._env.venue,
                    "trigger": trigger.value if trigger else None,
                },
            )

    async def _pod_token_refresh_loop(self) -> None:
        """Best-effort token rotation. The pod token is ~60-min and the worker reads ``$token_env``
        only at startup (``load_dotenv`` setdefault), so a long-lived worker would lose pod-sync.
        This re-reads the token from the .env FILE on a timer (a Mac launchd relay keeps it fresh —
        ``deploy/relay/``) and, when it changed, swaps a freshly-tokened pod client into the status
        writer + command source — no restart. TEST-8: pod-sync is never on the money path; every
        step is best-effort and self-healing, so a failure just retries next tick and trading,
        the kill-switch, the deadman, and reconcile are all untouched. The token value is NEVER
        logged."""
        cfg = self._env.pod_sync
        assert cfg is not None and self._pod_status is not None  # started only when both hold
        current = os.environ.get(cfg.token_env)  # the startup token the clients were built with
        while not self._control.stopped:
            await asyncio.sleep(cfg.token_refresh_seconds)
            try:
                token = await asyncio.to_thread(
                    read_envfile_token, cfg.token_envfile, cfg.token_env
                )
                if not token or token == current:
                    continue  # nothing staged yet, or unchanged since the last swap
                pod = build_pod_client(self._env, token=token)
                if pod is None:
                    continue  # build failed (logged inside) — keep the current client, retry later
                self._pod_status.set_pod(pod)
                if self._pod_command_source is not None:
                    self._pod_command_source.set_pod(pod)
                current = token
                self._log.info("pod_token_refreshed")  # the VALUE is never logged
            except Exception as exc:  # self-healing: rotation must never kill its own loop
                self._log.warning("pod_token_refresh_error", error=str(exc))

    def _status_detail(self) -> dict[str, object]:
        """Compact telemetry for the cockpit (money as str — never a float, B5)."""
        last = self._last_tick_at
        age = (self._now() - last).total_seconds() if last is not None else None
        return {
            "run_state": self._control.state.value,
            "strategy": self._env.strategy,
            "venue": self._env.venue,
            "halt_trigger": self._risk.halt_trigger.value if self._risk.halt_trigger else None,
            "realized_pnl": str(self._oms.total_realized_pnl()),
            "unrealized_pnl": str(self._oms.total_unrealized_pnl()),
            "open_positions": sum(1 for p in self._oms.positions if p.quantity != 0),
            "last_tick_age_s": round(age, 1) if age is not None else None,
        }

    async def _periodic(self) -> None:
        # NB: the heartbeat is beaten ONLY from the market loop (per tick), never here —
        # so a wedged market loop (no ticks) stops beating and the INDEPENDENT deadman
        # fires (TEST-5). The feed-stale self-trip is the in-band complement when a
        # position is open (a flat feed outage auto-recovers — see _check_feed_stale).
        while not self._control.stopped:
            await asyncio.sleep(self._env.reconcile_interval_seconds)
            # Reconcile FIRST so the feed-stale check sees the broker-truth book — a
            # position adopted from the broker this cycle is detected in-band now, not
            # one cycle later (a clean position adopts; genuine drift halts here).
            await self._reconcile()
            self._check_feed_stale()
            await self._maybe_flatten_on_halt()

    def _check_feed_stale(self) -> None:
        """A stale feed is only a *safety* event while a position is OPEN — then we
        are flying blind on live risk, so trip the LATCHING kill (TEST-4) and let
        ``handle_kill`` flatten (the independent deadman is the backstop, TEST-5).

        While FLAT, a stale feed protects nothing: there is nothing to flatten and no
        signal can be generated without ticks (the market loop is simply parked on the
        next tick). So we log and AUTO-RECOVER when ticks resume — never latching a
        halt that would need a manual re-arm. This matches the documented intent
        (``config/risk.yaml``: "no tick on a HELD symbol") and keeps the held-position
        guard tight for live, while letting a flaky paper feed (e.g. the Delta testnet
        REST poll) ride out transient outages during the idle no-trade soak."""
        if self._last_tick_at is None or self._risk.is_halted:
            return
        age = (self._now() - self._last_tick_at).total_seconds()
        if age <= self._feed_stale_seconds:
            return
        holding = any(p.quantity != 0 for p in self._oms.positions)
        if holding:
            self._log.error("feed_stale", age_seconds=age, holding=True)
            self._risk.trip(KillTrigger.FEED_STALE)
        else:
            # Flat soak: a flaky feed is not a risk event. Surface it, don't latch.
            self._log.warning("feed_stale_flat", age_seconds=age)

    async def _reconcile(self) -> None:
        # Atomic against the live book: holds the OMS lock across the snapshot + broker
        # fetch + adopt, so a concurrent submit/fill can't make the snapshot stale and
        # trip a SPURIOUS mismatch (review BLOCKER 1). The reconciler trips the kill on
        # a genuine divergence; audit a halt's issues for the post-mortem.
        report = await self._oms.reconcile_locked(self._reconciler)
        if report.status is not ReconcileStatus.CLEAN:
            await self._oms.audit_reconcile_halt(report.issues)

    async def _maybe_flatten_on_halt(self) -> None:
        """Flatten the book ONCE per distinct kill (cancel → flatten) and pause; it
        never silently resumes — a command ``clear_halt`` on a clean reconcile re-arms,
        and a *fresh* trip (new halt generation) flattens again. The generation is read
        + claimed synchronously before the await, so the concurrent market + periodic
        tasks can't both enter handle_kill for the same kill (cooperative scheduling)."""
        gen = self._risk.halt_generation
        if self._risk.is_halted and gen != self._handled_halt_generation:
            self._handled_halt_generation = gen  # claim before the await (no double-entry)
            self._control.set(RunState.PAUSED)
            await handle_kill(
                self._oms,
                self._risk.halt_trigger,
                notifier=self._notifier,
                drain=self._drain_inline,
            )

    async def rearm(self) -> tuple[bool, str]:
        """Offline gated re-arm of a LATCHED halt (no market loop runs) — for an
        operator clearing a worker stuck on a transient halt when the pod ``clear_halt``
        command channel isn't up. Rebuild state + restore the latched halt (as a cold
        start would), then re-arm ONLY on a clean reconcile (broker = truth) and PERSIST
        the clear so it survives the next restart. A dirty book leaves the halt latched.
        Returns ``(re_armed, detail)``. Closes the adapter on exit."""
        try:
            self._oms.rebuild_state()
            self._oms.restore_daily_state(self._today())
            if not self._risk.is_halted:
                return True, "not halted — nothing to re-arm"
            cleared = self._risk.halt_trigger  # capture before rearm_on_clean_reconcile clears it
            rearmed, detail = await rearm_on_clean_reconcile(
                self._oms, self._risk, self._reconciler
            )
            if rearmed:
                await self._oms.clear_persisted_halt(cleared)  # durable clear (survives restart)
                self._notifier.send("worker re-armed on clean reconcile", severity=Severity.WARNING)
            else:
                self._notifier.send(f"worker re-arm refused: {detail}", severity=Severity.WARNING)
            return rearmed, detail
        finally:
            await self._adapter.aclose()

    async def _shutdown(self) -> None:
        if self._drain_inline:
            await self._oms.drain_events()  # book any pending fills (bounded path only)
        for symbol in self._env.symbols:
            self._bars.flush(symbol)  # drop the forming bars
        self._log.info("worker_shutdown", worker_id=self._env.worker_id)
        await self._adapter.aclose()  # release the ccxt ws/http session


def _ensure_db_dir(state_db: str) -> None:
    """Create the parent dir of a sqlite file URL so a fresh box doesn't crash on
    ``unable to open database file`` (``:memory:`` and dir-less paths are no-ops)."""
    prefix = "sqlite:///"
    if not state_db.startswith(prefix):
        return
    parent = Path(state_db.removeprefix(prefix)).parent
    if str(parent) not in (".", ""):
        parent.mkdir(parents=True, exist_ok=True)


def build_worker(env: EnvConfig) -> Worker:
    """Construct the live paper/► worker from config + the active (gated) venue."""
    venues = load_venues()
    venue_cfg = active_venue(env, venues)  # live-gate check
    adapter = build_adapter(venue_cfg, env)  # live-gate check (defence in depth)
    risk_config = load_risk_config()
    risk = RiskManager(risk_config)
    _ensure_db_dir(env.state_db)
    store = StateStore(env.state_db)
    store.create_schema()
    clock = SystemClock()
    oms = OMS(adapter=adapter, risk=risk, store=store, venue=venue_cfg.venue, clock=clock)
    reconciler = Reconciler(adapter=adapter, risk=risk)
    engine = StrategyEngine(build_strategy(env.strategy))
    feed_stale = float(risk_config.kill_switch.triggers.feed_stale_seconds)
    control = WorkerControl()
    pod = build_pod_client(env)  # None unless pod_sync is configured + a token is staged
    pod_status = (
        PodStatusWriter(pod, worker_id=env.worker_id, mode=env.mode) if pod is not None else None
    )
    # The pod-backed command bus (cockpit -> worker). The worker is the sole executor
    # (TEST-8): the pod only ISSUES commands; this watcher applies them to the LOCAL risk
    # gate/OMS. drain=False matches drain_inline=False (consume_events books flatten fills).
    # Named so the token-rotation loop can swap its pod client alongside the status writer.
    pod_command_source = PodCommandSource(pod) if pod is not None else None
    command_watcher = (
        CommandWatcher(
            source=pod_command_source,
            oms=oms,
            risk=risk,
            control=control,
            worker_id=env.worker_id,
            reconciler=reconciler,
            drain=False,
        )
        if pod_command_source is not None
        else None
    )
    return Worker(
        env=env,
        adapter=adapter,
        oms=oms,
        risk=risk,
        reconciler=reconciler,
        engine=engine,
        bar_builder=BarBuilder(env.bar_interval_seconds),
        heartbeat=HeartbeatFile(env.heartbeat_path),
        control=control,
        feed=AdapterFeed(adapter),
        feed_stale_seconds=feed_stale,
        pod_command_source=pod_command_source,  # rotated alongside pod_status by the refresh loop
        # The CcxtAdapter's order_events() is ALWAYS continuous — a ccxt.pro ws stream
        # OR an infinite REST poll (Delta) — never a bounded sim that returns after
        # draining. So the worker ALWAYS consumes it via the long-lived consume_events
        # background task, never the inline drain_events: draining `async for`s over the
        # stream, which for the REST poll NEVER returns and wedges the market loop on the
        # first closed bar (the heartbeat freezes -> the deadman trips). drain_inline=True
        # is only for a bounded PaperBroker sim, which build_adapter never builds.
        drain_inline=False,
        command_watcher=command_watcher,  # pod->worker command bus (None unless configured)
        pod_status=pod_status,  # best-effort worker->pod heartbeat (None unless configured)
        clock=clock,
    )


async def run_worker(env_name: str = "paper") -> None:  # pragma: no cover - live entrypoint
    """Load the environment and run the worker (the deployable entrypoint body).

    Exercised by the deploy + the testnet self-test, not unit tests (it binds the
    real venue ws/REST and runs forever); ``build_worker`` + ``Worker`` are tested."""
    await build_worker(load_env_config(env_name)).run()
