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
from datetime import UTC, date, datetime
from pathlib import Path

from alpha_core.adapters.paper import PaperBroker
from alpha_core.core.enums import AssetClass
from alpha_core.core.errors import BrokerError
from alpha_core.core.interfaces import BrokerAdapter, DataFeed
from alpha_core.data.bar_builder import BarBuilder
from alpha_core.data.feed import AdapterFeed, TeeFeed
from alpha_core.execution.commands import CommandWatcher, RunState, WorkerControl
from alpha_core.execution.costs import CostModel
from alpha_core.execution.deadman import HeartbeatFile
from alpha_core.execution.funding import FundingConfig, funding_cash_flow, load_funding_config
from alpha_core.execution.instruments import InstrumentRegistry
from alpha_core.execution.oms import OMS
from alpha_core.execution.reconcile import Reconciler, ReconcileStatus
from alpha_core.execution.session import handle_kill, rearm_on_clean_reconcile
from alpha_core.execution.state import StateStore
from alpha_core.helpers.config import load_yaml
from alpha_core.observability.logging import get_logger
from alpha_core.observability.notify import LoggingNotifier, Notifier, Severity
from alpha_core.risk.limits import load_risk_config
from alpha_core.risk.manager import KillTrigger, RiskManager
from alpha_core.scheduler.clock import Clock, MarketSchedule, SystemClock, schedule_for
from alpha_core.strategy.engine import StrategyEngine
from alpha_core.strategy.registry import build_strategy
from worker.adapters import build_adapter, build_kite_ticker_feed
from worker.config import EnvConfig, active_venue, load_env_config, load_venues
from worker.pod_sync import (
    PodCommandSource,
    PodStatusWriter,
    PodTradeSync,
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
        pod_trade_sync: PodTradeSync | None = None,
        data_adapter: BrokerAdapter | None = None,
        schedule: MarketSchedule | None = None,
        clock: Clock | None = None,
        funding: FundingConfig | None = None,
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
        # Trade-coupled per-event sync (orders/fills/positions/pnl_snapshots), run from the
        # pod-status loop — best-effort, never on the money path (TEST-8). None = off (no
        # deployment_id configured).
        self._pod_trade_sync = pod_trade_sync
        # Paper mode's SEPARATE market-data adapter (ccxt), closed at shutdown alongside
        # the execution adapter; None when the feed owns its own transport (kite) or the
        # execution adapter IS the data source (venue mode).
        self._data_adapter = data_adapter
        # Session rules (SCHED-1, ADR 0010): None/24x7 = no gating (crypto). Otherwise
        # entries are blocked outside the session and past no_new_entry_time, and —
        # when the env opts in — the book squares off daily at square_off_time.
        self._schedule = schedule
        self._session_block_logged: str | None = None  # log-once key (reason@date)
        self._squared_off_on: date | None = None  # the last session date squared off
        self._clock = clock or SystemClock()
        # Perp funding accrual (R13): None = venue has no funding (spot/equity). The rate per
        # boundary comes from the VENUE (adapter.funding_rate); the config rate is only the
        # loudly-logged fallback when the venue can't answer.
        self._funding_cfg = funding
        self._last_funding_boundary: datetime | None = None
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
                if self._control.should_submit and not self._session_blocks_entry(now):
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
                await self._maybe_sync_trades()
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

    async def _maybe_sync_trades(self) -> None:
        """Push the trade-coupled tables (orders/fills/positions/pnl_snapshots) to the pod —
        from the telemetry loop, never the trading path (TEST-8). Orders AND fills are read
        from the durable store OFF the event loop (the derived-P&L truth, R11; store-sourced
        orders mean a fill whose order left the live book still syncs); the sync itself
        swallows every pod error, so this can only cost telemetry freshness."""
        if self._pod_trade_sync is None:
            return
        # Book scalars BEFORE the store reads: a fill booking in between then leaves the
        # interval's snapshot with the fee counted but its P&L not yet — equity UNDER-stated
        # for one cadence, never over-stated (the next interval's row is exact).
        realized = self._oms.total_realized_pnl()
        unrealized = self._oms.total_unrealized_pnl()
        funding = self._oms.total_funding()
        orders = await asyncio.to_thread(self._oms.all_orders)
        fills = await asyncio.to_thread(self._oms.all_fills)
        await self._pod_trade_sync.sync(
            now=self._now(),
            orders=orders,
            fills=fills,
            positions=self._oms.positions,
            realized=realized,
            unrealized=unrealized,
            funding=funding,
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
                if self._pod_trade_sync is not None:
                    self._pod_trade_sync.set_pod(pod)
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

    def _session_blocks_entry(self, now: datetime) -> bool:
        """True when the session rules block the strategy at ``now`` (SCHED-1):
        outside the trading session (holiday / pre-open / post-close — Kite pushes
        pre-open snapshot ticks that must not trade) or past ``no_new_entry_time``.
        NB the gate wraps ``process_bar`` itself, so strategy signals — entries AND
        exits — defer to the next session's first processed bar; only the halt and
        square-off flatteners still place orders in a blocked window. (Blocked-window
        bars never reach the strategy, so its state cannot desync from the book.)"""
        if self._schedule is None or self._schedule.is_24x7:
            return False
        if not self._schedule.is_open(now):
            reason = "session_closed"
        elif self._schedule.is_after_no_new_entry(now):
            reason = "no_new_entry_cutoff"
        else:
            return False
        key = f"{reason}@{now.astimezone(UTC).date().isoformat()}"
        if self._session_block_logged != key:  # once per reason-TRANSITION per day
            self._session_block_logged = key
            self._log.info("session_entry_blocked", reason=reason)
        return True

    async def _maybe_square_off(self, now: datetime) -> None:
        """Opt-in (``intraday_square_off``) daily square-off at the segment's
        ``square_off_time`` (SCHED-1): cancel working orders, flatten the book —
        ONCE per session date. NOT a kill: the risk gate stays armed and trading
        resumes next session (entries are already blocked past no_new_entry).

        PRE-LIVE GATE (review #160 SF3): before this flag ever runs against a real
        order API, ``flatten_all`` must net/skip symbols with an in-flight closing
        order — a daily-loss halt landing between the square-off SELL and its fill
        would otherwise size a SECOND flatten off the stale local book (net short
        overnight). Paper's inline drain closes that window today."""
        if not self._env.intraday_square_off or self._schedule is None:
            return
        if self._risk.is_halted or not self._schedule.is_at_or_after_square_off(now):
            return  # a halt owns the book via handle_kill, never two flatteners
        day = now.astimezone(UTC).date()
        if self._squared_off_on == day:
            return
        self._squared_off_on = day  # claim before the awaits (periodic re-entry safety)
        working = [o for o in self._oms.orders if not o.state.is_terminal]
        holding = any(p.quantity != 0 for p in self._oms.positions)
        if not working and not holding:
            return  # nothing to do; the claim still stops re-checks today
        self._log.info("intraday_square_off", working=len(working), holding=holding)
        try:
            await self._oms.cancel_all_working()
            await self._oms.flatten_all()
            if self._drain_inline:
                await self._oms.drain_events()  # book the closing fills now (bounded path)
                if any(p.quantity != 0 for p in self._oms.positions):
                    raise RuntimeError("book not flat after the square-off fills")
            # Success is announced AFTER the attempt, never before (review #160).
            self._notifier.send("intraday square-off: day book closed", severity=Severity.INFO)
        except Exception as exc:
            # The day stays CLAIMED (retrying blind is unsafe until flatten_all nets
            # in-flight closes — review #160 SF3, pre-live), so the operator MUST act:
            # the book may carry positions overnight.
            self._log.error("square_off_failed", error=repr(exc))
            self._notifier.send(
                f"intraday square-off FAILED — book may be open overnight: {exc}",
                severity=Severity.CRITICAL,
            )

    async def _periodic(self) -> None:
        # NB: the heartbeat is beaten ONLY from the market loop (per tick), never here —
        # so a wedged market loop (no ticks) stops beating and the INDEPENDENT deadman
        # fires (TEST-5). The feed-stale self-trip is the in-band complement when a
        # position is open (a flat feed outage auto-recovers — see _check_feed_stale).
        while not self._control.stopped:
            await asyncio.sleep(self._env.reconcile_interval_seconds)
            try:
                # Reconcile FIRST so the feed-stale check sees the broker-truth book — a
                # position adopted from the broker this cycle is detected in-band now, not
                # one cycle later (a clean position adopts; genuine drift halts here).
                await self._reconcile()
                await self._accrue_funding()  # after reconcile: on the broker-truth book
                self._check_feed_stale()
                await self._maybe_flatten_on_halt()
                await self._maybe_square_off(self._now())
            except BrokerError as exc:
                # a transient venue error degrades ONE cycle — it must never silently
                # kill this task (reconcile cadence, feed-stale, and halt-flatten all
                # live here; the deadman only covers the market loop's heartbeat).
                self._log.warning("periodic_cycle_error", error=repr(exc))

    def _funding_boundary(self, now: datetime) -> datetime:
        """The most recent funding boundary at ``now`` — UTC-midnight-anchored every
        ``interval_hours`` (Binance/Delta perps fund at 00/08/16 UTC)."""
        assert self._funding_cfg is not None  # only called when funding is configured
        interval = self._funding_cfg.interval_hours
        utc_now = now.astimezone(UTC)
        return utc_now.replace(
            hour=(utc_now.hour // interval) * interval, minute=0, second=0, microsecond=0
        )

    async def _accrue_funding(self) -> None:
        """Accrue perp funding ONCE per crossed boundary on every held crypto position (R13):
        into realized P&L and the day's total, so a funding-bleed feeds the daily-loss kill
        via the next ``mark()``. The rate is the VENUE's settled rate for the boundary
        (``adapter.funding_rate``); when the venue can't answer, the configured assumed rate
        is used and loudly labelled. On startup the boundary baselines to the current one —
        a restart never double-accrues an interval (it may skip at most one; the broker's
        balance stays the reconcile truth either way). Likewise a stall spanning several
        boundaries accrues only the latest (the backtester catch-up-accrues each; live, a
        multi-hour stall means a dead worker the deadman has long since flattened)."""
        if self._funding_cfg is None:
            return
        boundary = self._funding_boundary(self._now())
        if self._last_funding_boundary is None:
            self._last_funding_boundary = boundary  # baseline: accrue from the NEXT boundary
            return
        if boundary == self._last_funding_boundary:
            return
        self._last_funding_boundary = boundary
        for position in self._oms.positions:
            if position.quantity == 0 or position.asset_class is not AssetClass.CRYPTO:
                continue
            mark = position.last_price or position.average_price
            if mark is None:  # pragma: no cover - qty!=0 guarantees average_price (model)
                continue
            rate = await self._adapter.funding_rate(position.symbol, boundary)
            source = "venue"
            if rate is None:
                rate = self._funding_cfg.rate
                source = "config_fallback"
            cash_flow = funding_cash_flow(position, mark, rate)
            self._oms.accrue_funding(cash_flow)
            self._log.info(
                "funding_accrued",
                symbol=position.symbol,
                boundary=boundary.isoformat(),
                rate=str(rate),
                source=source,
                cash_flow=str(cash_flow),
            )

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
        now = self._now()
        if self._schedule is not None and not self._schedule.is_open(now):
            # A closed session's silence is EXPECTED (SCHED-1): without this, any
            # position held overnight (the documented CNC default) would trip the
            # FEED_STALE kill ~90s after the 15:30 close, get flattened at the last
            # quote, and latch a halt needing a manual re-arm every single day.
            return
        age = (now - self._last_tick_at).total_seconds()
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
        if self._data_adapter is not None:
            await self._data_adapter.aclose()  # paper mode's separate data leg


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
    """Construct the live paper/► worker from config + the active (gated) venue.

    Two execution modes (M4.5): ``venue`` (the venue's own order API — the M3 path,
    testnet/live per the gate) and ``paper`` (the LIVE feed drives the ``PaperBroker``:
    real quotes, simulated fills through the cost model, no order ever leaves the
    process — live-feed paper, R9). Under paper, the broker-of-record for the OMS,
    reconciler, and shutdown IS the paper broker, so reconcile-truth and the halt
    machinery run identically against the simulated book."""
    venues = load_venues()
    venue_cfg = active_venue(env, venues)  # live-gate check (kite => paper-only enforced here)
    _ensure_db_dir(env.state_db)
    store = StateStore(env.state_db)
    store.create_schema()
    drain_inline = False
    feed: DataFeed
    data_adapter: BrokerAdapter | None = None  # a separate data leg to close at shutdown
    if env.execution == "paper":
        registry = InstrumentRegistry.from_config()
        paper = PaperBroker(
            cost_model=CostModel(load_yaml("costs.yaml")),
            instruments={s: registry.get(s).cost_meta() for s in env.symbols},
            starting_cash=env.paper_starting_cash,
        )
        # Restart continuity: the simulated book IS the venue, so it must re-home the
        # durable store's orders/positions or the startup gate reads phantom drift
        # (position at the store, nothing broker-side) and latches an unclearable halt.
        with store.transaction() as _s:
            paper.seed_from_store(store.load_orders(_s), store.load_positions(_s))
        adapter: BrokerAdapter = paper
        # The DATA leg: kite's ticker feed, or a (gated) ccxt venue's tick stream —
        # either way TeeFeed splices every tick into the paper broker's quotes, so
        # simulated fills price off exactly the stream the strategy saw.
        if venue_cfg.adapter == "kite":
            data_feed: DataFeed = build_kite_ticker_feed(venue_cfg, env)
        else:
            data_adapter = build_adapter(venue_cfg, env)
            data_feed = AdapterFeed(data_adapter)
        feed = TeeFeed(data_feed, paper.on_tick)
        # PaperBroker's order_events() is BOUNDED (drains pending fills, returns) —
        # the inline drain after each bar is the correct consumption (see the ctor note).
        drain_inline = True
    else:
        adapter = build_adapter(venue_cfg, env)  # live-gate check (defence in depth)
        feed = AdapterFeed(adapter)
    risk_config = load_risk_config()
    risk = RiskManager(risk_config)
    # Session rules by venue market type (SCHED-1): Indian equity trades the NSE
    # session + holiday calendar; crypto market types are 24x7 (no gating).
    schedule = (
        schedule_for(frozenset({AssetClass.EQUITY})) if venue_cfg.market_type == "equity" else None
    )
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
    # Trade-coupled per-event sync — only when the deployment's pod row id is configured
    # (the FK every trade table requires). Best-effort telemetry, off the money path (TEST-8).
    pod_trade_sync = (
        PodTradeSync(
            pod,
            deployment_id=env.pod_sync.deployment_id,
            snapshot_seconds=env.pod_sync.pnl_snapshot_seconds,
        )
        if pod is not None and env.pod_sync is not None and env.pod_sync.deployment_id
        else None
    )
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
        feed=feed,
        feed_stale_seconds=feed_stale,
        pod_command_source=pod_command_source,  # rotated alongside pod_status by the refresh loop
        pod_trade_sync=pod_trade_sync,  # trade-coupled tables (None unless deployment_id set)
        # execution=venue: the CcxtAdapter's order_events() is ALWAYS continuous — a
        # ccxt.pro ws stream OR an infinite REST poll (Delta) — so it MUST be consumed by
        # the long-lived consume_events task, never the inline drain (which would `async
        # for` a never-ending stream and wedge the market loop on the first closed bar;
        # the heartbeat freezes -> the deadman trips). execution=paper: PaperBroker's
        # order_events() is BOUNDED (drains pending fills, returns), so the inline drain
        # after each bar is the correct consumption. The flag is set with the feed above.
        drain_inline=drain_inline,
        command_watcher=command_watcher,  # pod->worker command bus (None unless configured)
        pod_status=pod_status,  # best-effort worker->pod heartbeat (None unless configured)
        data_adapter=data_adapter,  # the paper mode's separate data leg (closed at shutdown)
        schedule=schedule,  # session gating (None = 24x7 crypto)
        clock=clock,
        # Perp funding accrual (R13) — only derivatives venues fund; a spot/equity venue
        # gets None (no accrual). Rates come from the venue at each boundary; the
        # costs.yaml rate is the loudly-logged fallback.
        funding=(
            load_funding_config(load_yaml("costs.yaml"))
            if venue_cfg.market_type == "swap"
            else None
        ),
    )


async def run_worker(env_name: str = "paper") -> None:  # pragma: no cover - live entrypoint
    """Load the environment and run the worker (the deployable entrypoint body).

    Exercised by the deploy + the testnet self-test, not unit tests (it binds the
    real venue ws/REST and runs forever); ``build_worker`` + ``Worker`` are tested."""
    await build_worker(load_env_config(env_name)).run()
