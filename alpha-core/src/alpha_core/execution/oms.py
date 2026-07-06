"""Order Management System (ADR 0003/0004/0006).

The OMS is the single path from an approved signal to a venue. It:
- derives a deterministic, idempotent ``client_order_id`` from a signal;
- runs the non-bypassable risk gate before any order is built;
- drives the order through the FSM (`order_fsm.step`) on submit and on every
  broker event, persisting each transition to the append-only state store;
- maintains local positions/P&L from fills (via the shared `apply_fill`);
- routes FSM conflicts / unknown orders to the kill switch (reconcile in B5.4).
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from decimal import Decimal

from alpha_core.core.enums import OrderState, OrderType, Side, Venue
from alpha_core.core.errors import OrderRejected, TerminalBrokerError, TransientBrokerError
from alpha_core.core.interfaces import BrokerAdapter, BrokerEventKind, BrokerOrderEvent
from alpha_core.core.models import Fill, Order, Position, Signal
from alpha_core.core.order_fsm import (
    OrderEvent,
    OrderStateConflict,
    UnknownOrderError,
    _fill_key,
    step,
)
from alpha_core.execution.instruments import InstrumentRegistry
from alpha_core.execution.positions import apply_fill
from alpha_core.execution.reconcile import Reconciler, ReconcileReport, ReconcileStatus
from alpha_core.execution.state import StateStore
from alpha_core.observability import metrics
from alpha_core.observability.logging import get_logger
from alpha_core.risk.manager import KillTrigger, RiskDecision, RiskManager, WorkingExposure
from alpha_core.scheduler.clock import Clock, SystemClock

_KIND_TO_EVENT = {
    BrokerEventKind.FILL: OrderEvent.FILL,
    BrokerEventKind.REJECT: OrderEvent.REJECT,
    BrokerEventKind.CANCEL: OrderEvent.CANCEL,
    BrokerEventKind.EXPIRE: OrderEvent.EXPIRE,
}


def derive_client_order_id(signal: Signal, venue: Venue) -> str:
    """Deterministic, venue-safe id, stable across retries of the same intent."""
    parts = "|".join(
        str(x)
        for x in (
            signal.strategy_id,
            signal.symbol,
            venue.value,
            signal.side.value,
            signal.order_type.value,
            signal.quantity,
            signal.limit_price,
            signal.stop_price,
            signal.created_at.isoformat(),
        )
    )
    digest = hashlib.sha1(parts.encode()).hexdigest()[:16]
    return f"alpha-{digest}"


def derive_client_order_id_for_flatten(pos: Position, venue: Venue, now: datetime) -> str:
    """Deterministic id for a kill-flatten order (stable if the same flatten retries)."""
    parts = "|".join(
        str(x) for x in ("flatten", pos.symbol, venue.value, pos.quantity, now.isoformat())
    )
    return f"alpha-{hashlib.sha1(parts.encode()).hexdigest()[:16]}"


class OMS:
    """Owns order lifecycle, local books, and the risk/FSM/state wiring."""

    def __init__(
        self,
        *,
        adapter: BrokerAdapter,
        risk: RiskManager,
        store: StateStore,
        venue: Venue,
        instruments: InstrumentRegistry | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._adapter = adapter
        self._risk = risk
        self._store = store
        self._venue = venue
        self._instruments = instruments  # gates the universe + quantizes (G9)
        # All OMS time comes from here — wall-clock live (default), bar time in
        # backtest — so the throttle + FSM stamps stay deterministic (G23).
        self._clock = clock or SystemClock()
        self._orders: dict[str, Order] = {}
        self._positions: dict[tuple[Venue, str], Position] = {}
        self._seen_fills: set[str] = set()
        self._day_realized: Decimal = Decimal(0)
        self._day_date: str | None = None  # the trading date _day_realized accumulates for
        self._funding: Decimal = Decimal(0)  # cumulative perp funding cash flow (R13)
        # Reservation price per working order (for valuing market orders that have
        # no limit price) + the single lock that makes check→reserve→place→ack
        # atomic against fill application (ADR 0014).
        self._working_price: dict[str, Decimal] = {}
        self._lock = asyncio.Lock()
        self._log = get_logger("oms")

    def now(self) -> datetime:
        """The OMS clock instant — the one time source for OMS-driven decisions."""
        return self._clock.now()

    _WORKING_STATES = (OrderState.PENDING, OrderState.OPEN, OrderState.PARTIALLY_FILLED)

    def _working_exposure(self) -> list[WorkingExposure]:
        """In-flight reservations: each non-terminal order's unfilled remainder,
        valued at its limit price (or the reference price captured at submit for a
        market order). Fed to the risk gate so concurrent orders count (ADR 0014)."""
        out: list[WorkingExposure] = []
        for cid, o in self._orders.items():
            if o.state not in self._WORKING_STATES:
                continue
            remaining = o.quantity - o.filled_quantity
            if remaining <= 0:
                continue
            price = o.limit_price or self._working_price.get(cid)
            if price is None or price <= 0:
                continue
            out.append(
                WorkingExposure(
                    venue=o.venue,
                    symbol=o.symbol,
                    asset_class=o.asset_class,
                    side=o.side,
                    remaining_qty=remaining,
                    price=price,
                )
            )
        return out

    @staticmethod
    def _trading_date(ts: datetime) -> str:
        return ts.astimezone(UTC).date().isoformat()

    def _roll_trading_date(self, ts: datetime) -> None:
        """Reset the DAILY realized counter on a trading-date (UTC) rollover.

        'Daily loss' is per trading DATE (ADR 0006). Without this reset a long-lived
        process accumulates every prior day's realized (fees included) into today's
        halt math — the kill fires on the cumulative bleed instead of a daily one
        (found live by the F1 fold: all four 11-year tracks latched in Feb-2021 on
        ~-2% of accumulated round-trip costs, review #179 aftermath). The halt
        itself still LATCHES across days — resetting the counter never re-arms."""
        date = self._trading_date(ts)
        # FORWARD-only (ISO dates order lexicographically): a stale venue fill.ts from
        # just before midnight must never roll the window BACKWARD — that would wipe
        # today's accumulation (a fail-OPEN kill) and re-key rows onto yesterday
        # (review #180 MAJOR, reproduced). A stale delta lands in the CURRENT window
        # instead — conservative in the only direction a kill may err.
        if self._day_date is None or date > self._day_date:
            self._day_date = date
            self._day_realized = Decimal(0)

    def restore_daily_state(self, trading_date: str) -> None:
        """Restore today's realized-P&L baseline + latched halt on restart (ADR 0006)."""
        with self._store.transaction() as s:
            row = self._store.load_daily_pnl(s, trading_date)
        if row is None:
            self._day_date = trading_date  # seat the window even with no row yet
            return
        self._day_date = trading_date
        self._day_realized = row.realized_pnl
        if row.halted:
            trigger = KillTrigger(row.halt_trigger) if row.halt_trigger else KillTrigger.MANUAL
            self._risk.restore(halted=True, trigger=trigger)

    def rebuild_state(self) -> None:
        """Reconstruct positions + the fill-dedup set by replaying the append-only
        fills (disaster-recovery path, RB-12).

        Fills are the source of truth; this defends against a lost or corrupt
        positions cache. Replays every fill through the same `apply_fill` math the
        live path uses, so the rebuilt positions carry the correct average price
        and realized P&L. Also restores the resting (non-terminal) **orders** so the
        startup reconcile can diff them against the broker — without this they pass
        as `local_orders=[]` and any resting order looks unknown and halts (EXEC-3).
        Call on a cold start (paired with `restore_daily_state`, which restores the
        daily-P&L baseline + latched halt) before trading.
        """
        with self._store.transaction() as s:
            fills = self._store.load_fills(s)
            orders = self._store.load_orders(s)
        positions: dict[tuple[Venue, str], Position] = {}
        seen: set[str] = set()
        for fill in sorted(fills, key=lambda f: f.ts):
            key = (fill.venue, fill.symbol)
            positions[key] = apply_fill(positions.get(key), fill)
            seen.add(_fill_key(fill))  # same key the FSM dedups on (venue_fill_id or id), S3
        self._positions = positions
        self._seen_fills = seen
        # Only resting orders need re-tracking; terminal ones get no further events.
        self._orders = {o.client_order_id: o for o in orders if not o.state.is_terminal}
        self._working_price = {
            cid: o.limit_price for cid, o in self._orders.items() if o.limit_price is not None
        }
        self._log.info(
            "state_rebuilt", fills=len(fills), positions=len(positions), orders=len(self._orders)
        )

    async def reconcile_locked(self, reconciler: Reconciler) -> ReconcileReport:
        """Run a reconcile **atomically against the live book** (ADR 0009/0014).

        Holds the OMS lock across the local snapshot + the broker fetch + the adopt,
        so a concurrent submit/fill can't make the snapshot stale between the read and
        the broker truth — which would otherwise look like drift and trip a *spurious*
        reconciliation halt (the periodic-reconcile race). A genuine divergence still
        trips (the snapshot is consistent, not blind). Costs the broker round-trip on
        the lock, briefly pausing submits — the correctness trade the live loop wants.
        """
        async with self._lock:
            report = await reconciler.reconcile(
                local_orders=self.orders, local_positions=self.positions
            )
            if report.status is ReconcileStatus.CLEAN:
                await self._apply_adoptions(report.adopted_orders, report.adopted_positions)
        return report

    async def apply_reconciliation(
        self, *, adopted_orders: list[Order], adopted_positions: list[Position]
    ) -> None:
        """Apply a CLEAN reconcile's adoptions to the local book (ADR 0009, F6).

        The broker is the source of truth: adopt its order and position records
        into the in-memory book and persist each with an audit row. The reconciler
        *computes* these for a recovered miss (missed ack/fill, never-placed); this
        is the consumer that actually **applies** them — without it the local book
        stays known-divergent after a recovered miss (the F6 bug). A no-op when
        there is nothing to adopt, and idempotent (re-adopting sets the same
        records). Held under the OMS lock so it serializes with submits/fills
        (ADR 0014). Only call it for a CLEAN report — a HALTED one adopts nothing.
        Prefer ``reconcile_locked`` for the live periodic reconcile (atomic snapshot)."""
        if not adopted_orders and not adopted_positions:
            return
        async with self._lock:
            await self._apply_adoptions(adopted_orders, adopted_positions)

    async def _apply_adoptions(
        self, adopted_orders: list[Order], adopted_positions: list[Position]
    ) -> None:
        """Adopt broker truth into the book + persist. **The caller must hold the lock.**"""
        if not adopted_orders and not adopted_positions:
            return
        for order in adopted_orders:
            self._orders[order.client_order_id] = order
            if order.state.is_terminal:
                self._working_price.pop(order.client_order_id, None)
            await asyncio.to_thread(self._persist_adopted_order, order)
        for position in adopted_positions:
            self._positions[(position.venue, position.symbol)] = position
            await asyncio.to_thread(self._persist_adopted_position, position)
        self._log.info(
            "reconcile_applied",
            orders=len(adopted_orders),
            positions=len(adopted_positions),
        )

    @property
    def positions(self) -> list[Position]:
        return list(self._positions.values())

    @property
    def orders(self) -> list[Order]:
        """The current order book — what the reconciler diffs against broker truth
        (ADR 0009). Includes resting (working) orders and any not-yet-pruned terminal ones."""
        return list(self._orders.values())

    def all_fills(self) -> list[Fill]:
        """The immutable fill history from the durable store (append-only; the derived-P&L
        truth, R11) — for read-only consumers (pod telemetry). BLOCKING (a DB read): call
        off the event loop (``asyncio.to_thread``), like every store write here."""
        with self._store.transaction() as s:
            return self._store.load_fills(s)

    def all_orders(self) -> list[Order]:
        """The full order history from the durable store (``save_order`` upserts by client
        id and never deletes) — for read-only consumers (pod telemetry): a fill whose order
        was pruned from the live book (or predates this process) still finds its order row.
        BLOCKING (a DB read): call off the event loop (``asyncio.to_thread``)."""
        with self._store.transaction() as s:
            return self._store.load_orders(s)

    def total_realized_pnl(self) -> Decimal:
        return sum((p.realized_pnl for p in self._positions.values()), Decimal(0)) + self._funding

    def total_funding(self) -> Decimal:
        """Cumulative perp funding cash flow accrued so far (negative = paid out, R13)."""
        return self._funding

    def accrue_funding(self, cash_flow: Decimal) -> None:
        """Book one funding payment (R13): into realized P&L (so it shows in reported
        P&L) AND into the day's realized total, so a funding-bleed feeds the daily-loss
        kill gate via the next ``mark()`` -> ``risk.update_pnl``. Durable + audited: a
        realized-P&L-affecting cash event must survive a restart (``restore_daily_state``
        re-seats a funding-inclusive day total) and leave an audit row (CLAUDE.md —
        every decision audited); ``now`` is the injected clock (bar-time in backtest)."""
        self._funding += cash_flow
        now = self._clock.now()
        self._roll_trading_date(now)
        self._day_realized += cash_flow
        trigger = self._risk.halt_trigger
        with self._store.transaction() as s:
            self._store.upsert_daily_pnl(
                s,
                trading_date=self._trading_date(now),
                day_start_equity=self._risk.base_capital,
                realized=self._day_realized,
                unrealized=self.total_unrealized_pnl(),
                halted=self._risk.is_halted,
                halt_trigger=trigger.value if trigger else None,
                updated_at=now,
            )
            self._store.append_audit(
                s,
                event_type="FUNDING_ACCRUED",
                payload={
                    "cash_flow": str(cash_flow),
                    "cumulative_funding": str(self._funding),
                    "day_realized": str(self._day_realized),
                },
                ts=now,
            )

    def total_unrealized_pnl(self) -> Decimal:
        """Mark-to-market P&L on open positions from their last marks."""
        total = Decimal(0)
        for p in self._positions.values():
            if p.quantity != 0 and p.average_price is not None and p.last_price is not None:
                total += (p.last_price - p.average_price) * p.quantity
        return total

    def mark(self, prices: dict[str, Decimal]) -> None:
        """Update last-marks for open positions and re-check the daily-loss kill
        switch against realized + unrealized P&L (ADR 0006). Drawdown on an open
        position can trip the switch even with no new fill."""
        self._roll_trading_date(self._clock.now())
        for (venue, symbol), pos in self._positions.items():
            price = prices.get(symbol)
            if price is not None and pos.quantity != 0:
                self._positions[(venue, symbol)] = pos.model_copy(update={"last_price": price})
        self._risk.update_pnl(realized=self._day_realized, unrealized=self.total_unrealized_pnl())

    # --- submit ----------------------------------------------------------------

    async def _place_idempotent(self, order: Order) -> str:
        """Place `order`, made idempotent by dedup-by-query (ADR 0004 H8).

        A transient failure leaves it ambiguous whether the venue received the
        order, so before surfacing the failure we query the venue by client id:
        if it already landed, we adopt its `venue_order_id` rather than let the
        order look failed — a blind retry would otherwise duplicate it. If the
        venue has no such order the placement truly failed, so we re-raise and
        leave the order PENDING for reconcile to resolve.
        """
        try:
            return await self._adapter.place_order(order)
        except TransientBrokerError:
            existing = await self._adapter.find_order_id(order.client_order_id)
            if existing is not None:
                self._log.warning(
                    "placement_deduped",
                    client_order_id=order.client_order_id,
                    venue_order_id=existing,
                )
                return existing
            raise

    async def submit_signal(self, signal: Signal, *, reference_price: Decimal) -> Order | None:
        """Risk-check a signal and, if approved, place the order (idempotent).

        Returns the placed `Order`, or `None` if risk rejected it.

        Held under the OMS lock so the risk check, the in-flight reservation, and
        the placement form one critical section against concurrent fill
        application — a fill can't slip in between the check and the ACK (ADR 0014).
        """
        async with self._lock:
            return await self._submit_locked(signal, reference_price)

    async def _submit_locked(self, signal: Signal, reference_price: Decimal) -> Order | None:
        now = self._clock.now()
        cid = derive_client_order_id(signal, self._venue)
        # Universe gate + lot/tick quantization (G9). The client id is derived from
        # the signal intent, so quantizing the order keeps it idempotent.
        quantity, limit_price, stop_price = signal.quantity, signal.limit_price, signal.stop_price
        if self._instruments is not None:
            if not self._instruments.is_known(signal.symbol):
                await asyncio.to_thread(self._reject_pre_trade, cid, signal, "unknown instrument")
                return None
            spec = self._instruments.get(signal.symbol)
            quantity = spec.round_quantity(quantity)
            if quantity <= 0:
                await asyncio.to_thread(
                    self._reject_pre_trade, cid, signal, "quantity below one lot"
                )
                return None
            limit_price = spec.round_price(limit_price) if limit_price is not None else None
            stop_price = spec.round_price(stop_price) if stop_price is not None else None
        if signal.reduce_only:
            # Size to the live book: a reduce-only intent can only CLOSE. Flat (or
            # already the wrong side) -> no order at all — an exit signal can never
            # open the inverse position (#179's bug class, closed structurally).
            held = next(
                (
                    pos.quantity
                    for pos in self.positions
                    if pos.symbol == signal.symbol and pos.quantity != 0
                ),
                Decimal(0),
            )
            closeable = held if signal.side is Side.SELL else -held
            if closeable <= 0:
                self._log.info("reduce_only_noop", client_order_id=cid, symbol=signal.symbol)
                return None
            quantity = min(quantity, closeable)
        order = Order(
            client_order_id=cid,
            symbol=signal.symbol,
            venue=self._venue,
            asset_class=signal.asset_class,
            side=signal.side,
            order_type=signal.order_type,
            quantity=quantity,
            limit_price=limit_price,
            stop_price=stop_price,
            post_only=signal.post_only,
            reduce_only=signal.reduce_only,
            valid_until=signal.valid_until,
            state=OrderState.NEW,
            strategy_id=signal.strategy_id,
            created_at=now,
            updated_at=now,
        )
        decision: RiskDecision = self._risk.check_order(
            order,
            reference_price=reference_price,
            positions=self.positions,
            now=now,
            working=self._working_exposure(),  # in-flight reservations (ADR 0014)
        )
        await asyncio.to_thread(self._audit_decision, cid, signal.strategy_id, decision)
        if not decision.approved:
            metrics.risk_decisions.labels(outcome="rejected").inc()
            metrics.orders_rejected.labels(reason="risk").inc()
            self._log.info("risk_rejected", client_order_id=cid, reason=decision.reason)
            return None
        metrics.risk_decisions.labels(outcome="approved").inc()
        return await self._place_approved(order, now, reference_price)

    async def _place_approved(self, order: Order, now: datetime, reserve_price: Decimal) -> Order:
        """Submit→place→ACK an order that has cleared (or bypasses) the risk gate.

        Shared by ``submit_signal`` (after approval) and ``flatten_all`` (a kill
        flatten, which bypasses the gate by design). Drives the FSM, persists
        each transition, and routes broker rejections to terminal state. ``now``
        (the OMS clock) stamps every FSM transition. The order is **reserved the
        moment it goes PENDING** (recorded in ``_orders`` + ``_working_price``
        before the placement await), so a concurrent check counts it (ADR 0014);
        a rejection releases the reservation.
        """
        order = step(order, OrderEvent.SUBMIT, now=now)  # NEW -> PENDING
        cid = order.client_order_id
        self._orders[cid] = order  # reserve at submit (in-flight exposure)
        self._working_price[cid] = reserve_price
        await asyncio.to_thread(self._persist_order, order, event="ORDER_SUBMIT")
        try:
            venue_order_id = await self._place_idempotent(order)
        except (OrderRejected, TerminalBrokerError) as exc:
            order = step(order, OrderEvent.REJECT, now=now)
            self._orders[cid] = order  # terminal -> dropped from the working set
            self._working_price.pop(cid, None)  # release the reservation
            await asyncio.to_thread(
                self._persist_order, order, event="ORDER_REJECT", extra={"reason": str(exc)}
            )
            if isinstance(exc, OrderRejected) and exc.reason == "post_only":
                # A post-only order that would cross is a MISSED ENTRY — an expected
                # business outcome of maker execution, never an error: it must not
                # stride toward the consecutive-errors kill.
                metrics.orders_rejected.labels(reason="post_only").inc()
                self._log.info("post_only_missed", client_order_id=cid)
                return order
            metrics.orders_rejected.labels(reason="broker").inc()
            self._risk.record_error()
            return order

        order = step(order, OrderEvent.ACK, now=now, venue_order_id=venue_order_id)  # -> OPEN
        self._orders[cid] = order
        await asyncio.to_thread(self._persist_order, order, event="ORDER_ACK")
        metrics.orders_placed.labels(venue=self._venue.value, strategy=order.strategy_id).inc()
        self._risk.record_success()
        return order

    async def flatten_all(self) -> list[Order]:
        """Flatten every open position with opposing market orders, **bypassing
        the new-order halt** (ADR 0006/0010).

        A kill-switch flatten *is* the risk system acting, so it does not pass
        through ``check_order`` (which blocks while halted). This is the
        no-square-off kill behavior for 24/7 venues that have no session close
        to flatten at; the caller drains the resulting fills.
        """
        placed: list[Order] = []
        async with self._lock:  # serialize with submits/fills (ADR 0014)
            now = self._clock.now()
            for pos in list(self._positions.values()):
                if pos.quantity == 0:
                    continue
                side = Side.SELL if pos.quantity > 0 else Side.BUY
                reserve_price = pos.last_price or pos.average_price or Decimal(0)
                order = Order(
                    client_order_id=derive_client_order_id_for_flatten(pos, self._venue, now),
                    symbol=pos.symbol,
                    venue=self._venue,
                    asset_class=pos.asset_class,
                    side=side,
                    order_type=OrderType.MARKET,
                    quantity=abs(pos.quantity),
                    state=OrderState.NEW,
                    strategy_id="kill_flatten",
                    created_at=now,
                    updated_at=now,
                )
                self._log.warning(
                    "kill_flatten", symbol=pos.symbol, side=side.value, qty=str(order.quantity)
                )
                placed.append(await self._place_approved(order, now, reserve_price))
        return placed

    async def cancel_all_working(self) -> list[str]:
        """Cancel every working (non-terminal) order at the venue.

        Cancellation only ever *reduces* risk, so it is always allowed — even
        while halted. Idempotent (the adapter treats a terminal/absent order as a
        safe no-op). Returns the client ids cancelled; the caller drains the
        resulting CANCEL events.
        """
        cancelled: list[str] = []
        for cid, order in list(self._orders.items()):
            if not order.state.is_terminal:
                await self._adapter.cancel(cid)
                cancelled.append(cid)
        return cancelled

    # --- events ----------------------------------------------------------------

    async def drain_events(self) -> None:
        """Pull and handle all pending broker events (FILL/REJECT/CANCEL/EXPIRE).

        For bounded loops (paper/backtest) where fills are produced synchronously.
        """
        async for event in self._adapter.order_events():
            async with self._lock:  # serialize with submits (ADR 0014)
                await self.handle_event(event)

    async def consume_events(self) -> None:
        """Long-lived consumer of the broker's order-event stream (ADR 0004).

        For live adapters whose ``order_events()`` is a continuous websocket
        stream: runs until the stream ends or the task is cancelled. (For a
        bounded sim it simply drains the pending events and returns.) Run as a
        background task alongside the tick stream; cancel it on graceful shutdown.
        """
        async for event in self._adapter.order_events():
            async with self._lock:  # serialize with submits (ADR 0014)
                await self.handle_event(event)

    async def handle_event(self, event: BrokerOrderEvent) -> None:
        order = self._orders.get(event.client_order_id)
        if order is None:
            await self._on_conflict("unknown order", event.client_order_id)
            return
        fsm_event = _KIND_TO_EVENT[event.kind]
        try:
            order = step(
                order,
                fsm_event,
                now=self._clock.now(),
                fill=event.fill,
                seen_fills=self._seen_fills,
            )
        except (OrderStateConflict, UnknownOrderError) as exc:
            await self._on_conflict(str(exc), event.client_order_id)
            return
        self._orders[event.client_order_id] = order
        if order.state.is_terminal:
            self._working_price.pop(event.client_order_id, None)  # release reservation

        if event.kind is BrokerEventKind.FILL and event.fill is not None:
            fill = event.fill
            key = (fill.venue, fill.symbol)
            old = self._positions.get(key)
            old_realized = old.realized_pnl if old else Decimal(0)
            self._positions[key] = apply_fill(old, fill)
            delta = self._positions[key].realized_pnl - old_realized
            metrics.fills.labels(venue=fill.venue.value, side=fill.side.value).inc()
            self._roll_trading_date(fill.ts)
            self._day_realized += delta
            unrealized = self.total_unrealized_pnl()
            self._risk.update_pnl(realized=self._day_realized, unrealized=unrealized)
            trigger = self._risk.halt_trigger
            assert self._day_date is not None  # rolled above
            day_date = self._day_date  # the row must key the WINDOW's date, never a stale ts
            # In-memory book is updated above; the DB write runs OFF the event loop
            # (G21 / P17.3) so a slow disk/Postgres never stalls the feed/kill timing.
            await asyncio.to_thread(
                self._persist_fill,
                order,
                fill,
                self._positions[key],
                delta,
                unrealized,
                trigger,
                day_date,
            )
        else:
            await asyncio.to_thread(self._persist_order, order, event=f"ORDER_{event.kind.value}")

    def _persist_fill(
        self,
        order: Order,
        fill: Fill,
        position: Position,
        delta: Decimal,
        unrealized: Decimal,
        trigger: KillTrigger | None,
        trading_date: str,
    ) -> None:
        """One atomic fill write (order + fill + position + P&L + audit). Sync — run
        via ``asyncio.to_thread`` so the event loop never blocks on it (G21)."""
        with self._store.transaction() as s:
            self._store.save_order(s, order)
            self._store.append_fill(s, fill)
            self._store.upsert_position(s, position)
            if delta != 0:
                self._store.append_pnl(
                    s,
                    ts=fill.ts,
                    venue=fill.venue,
                    symbol=fill.symbol,
                    fill_id=fill.fill_id,
                    realized=delta,
                )
            self._store.upsert_daily_pnl(
                s,
                trading_date=trading_date,
                day_start_equity=self._risk.base_capital,
                realized=self._day_realized,
                unrealized=unrealized,
                halted=self._risk.is_halted,
                halt_trigger=trigger.value if trigger else None,
                updated_at=fill.ts,
            )
            self._store.append_audit(
                s,
                event_type="FILL",
                entity_id=order.client_order_id,
                payload={"qty": str(fill.quantity), "price": str(fill.price)},
            )

    # --- helpers ---------------------------------------------------------------

    def _audit_decision(self, cid: str, strategy_id: str, decision: RiskDecision) -> None:
        """Append the RISK_DECISION audit row. Sync — run via ``asyncio.to_thread``
        so the gate's verdict is durably recorded without blocking the loop (G21)."""
        with self._store.transaction() as s:
            self._store.append_audit(
                s,
                event_type="RISK_DECISION",
                entity_id=cid,
                strategy_id=strategy_id,
                payload={"approved": decision.approved, "reason": decision.reason},
            )

    def _reject_pre_trade(self, cid: str, signal: Signal, reason: str) -> None:
        """Audit + log an instrument-gate rejection (before the risk gate, G9)."""
        with self._store.transaction() as s:
            self._store.append_audit(
                s,
                event_type="PRE_TRADE_REJECT",
                entity_id=cid,
                strategy_id=signal.strategy_id,
                payload={"reason": reason, "symbol": signal.symbol},
            )
        metrics.orders_rejected.labels(reason="instrument").inc()
        self._log.warning(
            "pre_trade_rejected", client_order_id=cid, symbol=signal.symbol, reason=reason
        )

    def _persist_order(
        self, order: Order, *, event: str, extra: dict[str, object] | None = None
    ) -> None:
        with self._store.transaction() as s:
            self._store.save_order(s, order)
            self._store.append_audit(
                s,
                event_type=event,
                entity_id=order.client_order_id,
                strategy_id=order.strategy_id,
                payload={"state": order.state.value, **(extra or {})},
            )

    async def audit_reconcile_halt(self, issues: list[str]) -> None:
        """Persist a reconcile **HALT** decision to the append-only audit log (EXEC-2).

        A mismatch trips the kill (the halt itself is durable via the daily-P&L row),
        but the *issues* — which orders/positions diverged from broker truth —
        otherwise survive only in ephemeral structlog. CLAUDE.md requires every
        decision audited; this is the durable, hash-chained record a post-mortem
        reads. Recoveries are already audited per-item by ``apply_reconciliation``;
        a clean run decides nothing, so only a halt writes here.

        Held under the OMS lock so this write serializes with the in-lock audit writers
        — the lock is held across the ``to_thread`` (which commits), so two
        audit-bearing transactions never overlap and can't fork the hash chain by both
        reading the same prev-hash on a multi-connection pool (M3)."""
        async with self._lock:
            await asyncio.to_thread(self._persist_reconcile_halt, issues)

    def _persist_reconcile_halt(self, issues: list[str]) -> None:
        with self._store.transaction() as s:
            self._store.append_audit(s, event_type="RECONCILE_HALT", payload={"issues": issues})

    async def persist_halt(self, trigger: KillTrigger | None) -> None:
        """Persist the latched halt to today's daily-P&L row so it survives a restart
        (ADR 0006), even when the kill flattened nothing.

        The daily row is otherwise written only on a fill (``_persist_fill``), so a kill
        on a flat/position-less book (e.g. a daily-loss or consecutive-error trip with
        no open position) produced no fill and never persisted the halt — and
        ``restore_daily_state`` then saw no latch on restart, silently resuming trading.
        ``handle_kill`` calls this so every kill is durable. Idempotent (upsert).

        Also writes a dedicated ``KILL_SWITCH`` audit row — the kill is a *decision*,
        and CLAUDE.md requires every decision audited (the daily-P&L latch records the
        *state*, not a timestamped event; a flat-book kill otherwise left no audit-log
        entry at all). Held under the OMS lock so the hash-chained audit write serializes
        with the others and can't fork the chain (M3)."""
        async with self._lock:
            await asyncio.to_thread(self._persist_halt, trigger)

    def _persist_halt(self, trigger: KillTrigger | None) -> None:
        now = self.now()
        with self._store.transaction() as s:
            self._store.upsert_daily_pnl(
                s,
                trading_date=self._trading_date(now),
                day_start_equity=self._risk.base_capital,
                realized=self._day_realized,
                unrealized=self.total_unrealized_pnl(),
                halted=True,
                halt_trigger=trigger.value if trigger else None,
                updated_at=now,
            )
            self._store.append_audit(
                s,
                event_type="KILL_SWITCH",
                payload={"trigger": trigger.value if trigger else None},
            )

    async def clear_persisted_halt(self, cleared_trigger: KillTrigger | None = None) -> None:
        """Persist a re-armed (un-halted) state to today's daily-P&L row so a clean
        re-arm survives a restart — the symmetric counterpart to ``persist_halt``
        (ADR 0006). Without this an offline re-arm would clear the latch in memory but
        leave ``daily_pnl.halted=True``, so ``restore_daily_state`` would re-halt on the
        next boot. Writes a ``REARM`` audit row recording the halt that was cleared (the
        re-arm is a decision; symmetric with the ``KILL_SWITCH`` row's trigger).
        Idempotent; held under the OMS lock so the hash-chained audit write can't fork
        the chain. ``cleared_trigger`` is captured by the caller *before* ``rearm()``."""
        async with self._lock:
            await asyncio.to_thread(self._clear_persisted_halt, cleared_trigger)

    def _clear_persisted_halt(self, cleared_trigger: KillTrigger | None) -> None:
        now = self.now()
        with self._store.transaction() as s:
            self._store.upsert_daily_pnl(
                s,
                trading_date=self._trading_date(now),
                day_start_equity=self._risk.base_capital,
                realized=self._day_realized,
                unrealized=self.total_unrealized_pnl(),
                halted=False,
                halt_trigger=None,
                updated_at=now,
            )
            self._store.append_audit(
                s,
                event_type="REARM",
                payload={
                    "cleared_trigger": cleared_trigger.value if cleared_trigger else None,
                    "via": "clean reconcile",
                },
            )

    def _persist_adopted_order(self, order: Order) -> None:
        """Persist an order adopted from broker truth + its RECONCILE audit row (EXEC-2)."""
        with self._store.transaction() as s:
            self._store.save_order(s, order)
            self._store.append_audit(
                s,
                event_type="RECONCILE_ADOPT_ORDER",
                entity_id=order.client_order_id,
                strategy_id=order.strategy_id,
                payload={"state": order.state.value, "filled_qty": str(order.filled_quantity)},
            )

    def _persist_adopted_position(self, position: Position) -> None:
        """Persist a position adopted from broker truth + its RECONCILE audit row (EXEC-2)."""
        with self._store.transaction() as s:
            self._store.upsert_position(s, position)
            self._store.append_audit(
                s,
                event_type="RECONCILE_ADOPT_POSITION",
                entity_id=f"{position.venue.value}:{position.symbol}",
                payload={"qty": str(position.quantity), "avg_price": str(position.average_price)},
            )

    async def _on_conflict(self, detail: str, entity_id: str) -> None:
        """A broker callback contradicts local truth -> halt + alert. The audit write
        runs off the event loop (``to_thread``, G21) instead of blocking it on a sync
        DB write at the worst moment; it's already under the OMS lock (the caller,
        ``handle_event``, holds it), so it serializes with the other audit writers and
        can't fork the hash chain (M3)."""
        self._log.error("order_state_conflict", entity_id=entity_id, detail=detail)
        await asyncio.to_thread(self._persist_conflict_audit, entity_id, detail)
        metrics.kill_switch_trips.labels(trigger=KillTrigger.RECONCILIATION_MISMATCH.value).inc()
        self._risk.trip(KillTrigger.RECONCILIATION_MISMATCH)

    def _persist_conflict_audit(self, entity_id: str, detail: str) -> None:
        with self._store.transaction() as s:
            self._store.append_audit(
                s,
                event_type="RECONCILE_TRIGGER",
                entity_id=entity_id,
                payload={"detail": detail},
            )
