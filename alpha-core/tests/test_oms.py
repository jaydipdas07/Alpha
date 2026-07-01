"""OMS integration tests (Phase 5 B5.3) — signal -> risk -> FSM -> paper -> state."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from alpha_core.adapters.paper import PaperBroker
from alpha_core.core.enums import AssetClass, OrderState, OrderType, Side, Venue
from alpha_core.core.errors import BrokerTimeout
from alpha_core.core.interfaces import BrokerAdapter, BrokerEventKind, BrokerOrderEvent
from alpha_core.core.models import Fill, Order, Position, Signal, Tick
from alpha_core.execution.costs import CostModel, InstrumentMeta
from alpha_core.execution.instruments import InstrumentRegistry, InstrumentSpec
from alpha_core.execution.oms import OMS, derive_client_order_id
from alpha_core.execution.state import AuditRow, PnlLedgerRow, StateStore
from alpha_core.risk.limits import RiskConfig
from alpha_core.risk.manager import KillTrigger, RiskManager
from alpha_core.scheduler.clock import FakeClock

T0 = datetime(2026, 6, 15, 4, 0, tzinfo=UTC)
SYMBOL = "NSE:RELIANCE"

COST_CONFIG = {
    "slippage": {
        "equity": {"type": "bps", "value": 5},
        "crypto_perp": {"type": "bps", "value": 8},
        "index_option": {"type": "ticks", "value": 2},
        "default_spread": {"equity": 0.0005, "crypto_perp": 0.0008, "index_option_ticks": 1},
        "stress_multiplier": 2,
    },
    "segments": {
        "equity_intraday": {"brokerage": {"pct": 0.0003, "flat": 20, "mode": "min"}},
        "index_option": {"brokerage": {"flat": 20, "mode": "flat"}},
        "crypto_perp": {"trading_fee": {"pct": 0.001, "side": "both"}},
    },
}


def _risk(**overrides: object) -> RiskManager:
    limits = {
        "max_gross_exposure": "1.00",
        "max_position_per_instrument": "0.20",
        "max_concurrent_positions": 5,
        "max_order_value": "0.25",
        "max_orders_per_minute": 10,
        "max_daily_loss_halt": "0.02",
        "max_loss_per_trade": "0.01",
        "per_segment_exposure_cap": "0.60",
    }
    limits.update(overrides)
    cfg = RiskConfig.model_validate({"base_capital": "100000", "limits": limits})
    return RiskManager(cfg)


def _setup(
    risk: RiskManager | None = None, clock: object | None = None
) -> tuple[OMS, PaperBroker, StateStore]:
    broker = PaperBroker(
        cost_model=CostModel(COST_CONFIG),
        instruments={SYMBOL: InstrumentMeta(asset_class=AssetClass.EQUITY)},
        starting_cash=Decimal("1000000"),
    )
    broker.on_tick(
        Tick(
            symbol=SYMBOL,
            venue=Venue.NSE,
            asset_class=AssetClass.EQUITY,
            ts=T0,
            bid=Decimal("99"),
            ask=Decimal("101"),
            last_price=Decimal("100"),
        )
    )
    store = StateStore("sqlite:///:memory:")
    store.create_schema()
    oms = OMS(
        adapter=broker,
        risk=risk or _risk(),
        store=store,
        venue=Venue.NSE,
        clock=clock,  # type: ignore[arg-type]
    )
    return oms, broker, store


def _signal(qty: str = "10") -> Signal:
    return Signal(
        strategy_id="s1",
        symbol=SYMBOL,
        asset_class=AssetClass.EQUITY,
        side=Side.BUY,
        quantity=Decimal(qty),
        order_type=OrderType.MARKET,
        created_at=T0,
    )


# --- the headline: signal -> fill -> state -------------------------------------


async def test_signal_to_fill_updates_state() -> None:
    oms, _broker, store = _setup()
    order = await oms.submit_signal(_signal(), reference_price=Decimal("101"))
    assert order is not None
    assert order.state is OrderState.OPEN  # acked; fill arrives via the event stream
    await oms.drain_events()
    # order now filled; local position + persisted state updated
    assert oms.positions[0].quantity == Decimal("10")
    with store.transaction() as s:
        orders = store.load_orders(s)
        fills = store.load_fills(s)
        positions = store.load_positions(s)
    assert orders[0].state is OrderState.FILLED
    assert orders[0].filled_quantity == Decimal("10")
    assert len(fills) == 1
    assert positions[0].quantity == Decimal("10")


async def test_idempotent_client_order_id() -> None:
    sig = _signal()
    a = derive_client_order_id(sig, Venue.NSE)
    b = derive_client_order_id(sig, Venue.NSE)
    assert a == b and a.startswith("alpha-")


async def test_audit_chain_intact_after_flow() -> None:
    oms, _broker, store = _setup()
    await oms.submit_signal(_signal(), reference_price=Decimal("101"))
    await oms.drain_events()
    with store.transaction() as s:
        assert store.verify_audit_chain(s) is True


# --- risk rejection ------------------------------------------------------------


async def test_risk_rejection_returns_none() -> None:
    oms, _broker, store = _setup(risk=_risk(max_order_value="0.0001"))  # ₹10 cap
    order = await oms.submit_signal(_signal(qty="10"), reference_price=Decimal("101"))
    assert order is None
    with store.transaction() as s:
        assert store.load_orders(s) == []  # nothing placed


async def test_halted_blocks_submit() -> None:
    risk = _risk()
    risk.trip(KillTrigger.MANUAL)
    oms, _broker, _store = _setup(risk=risk)
    assert await oms.submit_signal(_signal(), reference_price=Decimal("101")) is None


# --- conflict -> kill switch ---------------------------------------------------


async def test_unknown_order_event_trips_kill_switch() -> None:
    risk = _risk()
    oms, _broker, _store = _setup(risk=risk)
    await oms.handle_event(BrokerOrderEvent(kind=BrokerEventKind.FILL, client_order_id="ghost"))
    assert risk.is_halted is True
    assert risk.halt_trigger is KillTrigger.RECONCILIATION_MISMATCH


async def test_conflict_audit_write_lands_off_loop_and_chain_intact() -> None:
    # M3: _on_conflict writes its RECONCILE_TRIGGER row off the event loop (to_thread)
    # under the OMS lock. Verify the audit row still lands, the kill trips, and the
    # append-only hash chain stays valid (the serialization that prevents a fork).
    oms, _broker, store = _setup()
    await oms.handle_event(BrokerOrderEvent(kind=BrokerEventKind.FILL, client_order_id="ghost"))
    assert oms._risk.is_halted is True
    with store.transaction() as s:
        events = [r.event_type for r in s.execute(select(AuditRow)).scalars()]
        assert store.verify_audit_chain(s) is True
    assert "RECONCILE_TRIGGER" in events


# --- P&L persistence + restart restore (H1) ------------------------------------


def _sell_signal(qty: str = "10") -> Signal:
    return Signal(
        strategy_id="s1",
        symbol=SYMBOL,
        asset_class=AssetClass.EQUITY,
        side=Side.SELL,
        quantity=Decimal(qty),
        order_type=OrderType.MARKET,
        created_at=T0 + timedelta(minutes=1),
    )


async def test_realizing_fill_persists_pnl() -> None:
    oms, _broker, store = _setup()
    await oms.submit_signal(_signal(), reference_price=Decimal("101"))
    await oms.drain_events()
    await oms.submit_signal(_sell_signal(), reference_price=Decimal("101"))  # realizes
    await oms.drain_events()
    with store.transaction() as s:
        ledger = list(s.execute(select(PnlLedgerRow)).scalars())
        daily = store.load_daily_pnl(s, OMS._trading_date(T0))
    assert len(ledger) >= 1  # the closing sell realized P&L
    assert daily is not None
    assert daily.realized_pnl == oms.total_realized_pnl()


async def test_mark_to_market_trips_daily_loss() -> None:
    # base 100000, daily loss 2% = 2000. Buy 100 @ ~100 then mark down to 80
    # -> unrealized ≈ -2000 -> kill switch trips with no new fill.
    risk = _risk()
    oms, _broker, _store = _setup(risk=risk)
    await oms.submit_signal(_signal(qty="100"), reference_price=Decimal("100"))
    await oms.drain_events()
    assert risk.is_halted is False
    oms.mark({SYMBOL: Decimal("80")})  # ~ -2000 unrealized
    assert risk.is_halted is True
    assert risk.halt_trigger is KillTrigger.DAILY_LOSS


async def test_mark_no_position_is_noop() -> None:
    risk = _risk()
    oms, _broker, _store = _setup(risk=risk)
    oms.mark({SYMBOL: Decimal("50")})  # no positions
    assert risk.is_halted is False
    assert oms.total_unrealized_pnl() == Decimal("0")


async def test_unrealized_pnl_sign() -> None:
    oms, _broker, _store = _setup()
    await oms.submit_signal(_signal(qty="10"), reference_price=Decimal("100"))
    await oms.drain_events()
    oms.mark({SYMBOL: Decimal("110")})  # long, price up -> positive unrealized
    assert oms.total_unrealized_pnl() > 0
    oms.mark({SYMBOL: Decimal("90")})  # price down -> negative
    assert oms.total_unrealized_pnl() < 0


# --- instrument registry: gate + quantize (G9) ---------------------------------


def _registry(lot: str = "1", tick: str = "0.05") -> InstrumentRegistry:
    spec = InstrumentSpec.model_validate(
        {
            "symbol": SYMBOL,
            "asset_class": AssetClass.EQUITY,
            "lot_size": Decimal(lot),
            "tick_size": Decimal(tick),
        }
    )
    return InstrumentRegistry({SYMBOL: spec})


def _setup_with_registry(reg: InstrumentRegistry) -> tuple[OMS, PaperBroker, StateStore]:
    oms, broker, store = _setup()
    oms._instruments = reg
    return oms, broker, store


async def test_unknown_instrument_rejected_at_gate() -> None:
    oms, _broker, store = _setup_with_registry(_registry())
    sig = _signal().model_copy(update={"symbol": "NSE:UNKNOWN"})
    assert await oms.submit_signal(sig, reference_price=Decimal("100")) is None
    with store.transaction() as s:
        assert store.load_orders(s) == []  # nothing placed


async def test_quantity_floored_to_lot() -> None:
    oms, _broker, _store = _setup_with_registry(_registry(lot="50"))
    # 125 -> 2 lots = 100
    order = await oms.submit_signal(_signal(qty="125"), reference_price=Decimal("101"))
    assert order is not None
    assert order.quantity == Decimal("100")


async def test_sub_lot_quantity_rejected() -> None:
    oms, _broker, store = _setup_with_registry(_registry(lot="50"))
    assert await oms.submit_signal(_signal(qty="40"), reference_price=Decimal("101")) is None
    with store.transaction() as s:
        assert store.load_orders(s) == []


async def test_limit_price_snapped_to_tick() -> None:
    oms, _broker, _store = _setup_with_registry(_registry(tick="0.05"))
    sig = _limit_signal(qty="10", limit="100.123")
    order = await oms.submit_signal(sig, reference_price=Decimal("100"))
    assert order is not None
    assert order.limit_price == Decimal("100.10")  # snapped to the 0.05 grid


# --- kill-switch auto-flatten (B10.2) ------------------------------------------


async def test_flatten_all_bypasses_halt() -> None:
    # The chosen 24/7 kill policy: a halted OMS blocks new signals but still
    # flattens open positions (flattening IS the risk system acting).
    risk = _risk()
    oms, _broker, _store = _setup(risk=risk)
    await oms.submit_signal(_signal(qty="10"), reference_price=Decimal("101"))
    await oms.drain_events()
    assert oms.positions[0].quantity == Decimal("10")

    risk.trip(KillTrigger.MANUAL)
    assert (
        await oms.submit_signal(_signal(qty="5"), reference_price=Decimal("101")) is None
    )  # blocked

    placed = await oms.flatten_all()
    await oms.drain_events()
    assert len(placed) == 1
    assert placed[0].side is Side.SELL
    assert placed[0].strategy_id == "kill_flatten"
    assert all(p.quantity == 0 for p in oms.positions)


async def test_flatten_all_covers_short() -> None:
    risk = _risk()
    oms, _broker, _store = _setup(risk=risk)
    await oms.submit_signal(_sell_signal(qty="10"), reference_price=Decimal("100"))  # go short
    await oms.drain_events()
    assert oms.positions[0].quantity == Decimal("-10")

    placed = await oms.flatten_all()
    await oms.drain_events()
    assert placed[0].side is Side.BUY  # buy to cover
    assert all(p.quantity == 0 for p in oms.positions)


async def test_flatten_all_no_positions_is_noop() -> None:
    oms, _broker, _store = _setup()
    assert await oms.flatten_all() == []


def _limit_signal(qty: str = "10", limit: str = "50") -> Signal:
    # A far-from-market BUY limit rests OPEN instead of filling.
    return Signal(
        strategy_id="s1",
        symbol=SYMBOL,
        asset_class=AssetClass.EQUITY,
        side=Side.BUY,
        quantity=Decimal(qty),
        order_type=OrderType.LIMIT,
        limit_price=Decimal(limit),
        created_at=T0,
    )


async def test_cancel_all_working_cancels_resting_orders() -> None:
    oms, _broker, _store = _setup()
    order = await oms.submit_signal(_limit_signal(), reference_price=Decimal("100"))
    assert order is not None and order.state is OrderState.OPEN  # resting, unfilled
    cancelled = await oms.cancel_all_working()
    await oms.drain_events()
    assert cancelled == [order.client_order_id]


async def test_handle_kill_cancels_and_flattens() -> None:
    from alpha_core.execution.session import handle_kill

    risk = _risk()
    oms, _broker, _store = _setup(risk=risk)
    # one filled position + one resting working order
    await oms.submit_signal(_signal(qty="10"), reference_price=Decimal("101"))
    await oms.drain_events()
    resting = await oms.submit_signal(_limit_signal(), reference_price=Decimal("100"))
    assert resting is not None
    risk.trip(KillTrigger.DAILY_LOSS)

    flattened = await handle_kill(oms, risk.halt_trigger)
    assert len(flattened) == 1  # the long was flattened
    assert all(p.quantity == 0 for p in oms.positions)
    # the resting order was cancelled (no longer working)
    assert all(o.state.is_terminal for o in oms._orders.values())


# --- idempotent dedup-by-query placement (H8) ----------------------------------


class _LostAckBroker(BrokerAdapter):
    """place_order lands at the venue but the ack is lost (transient error).

    ``landed`` controls whether a subsequent dedup query finds the order: True
    models a real lost-ack (the venue has it), False models a placement that
    genuinely never reached the venue.
    """

    def __init__(self, *, landed: bool) -> None:
        self.landed = landed
        self.place_calls = 0

    async def place_order(self, order: Order) -> str:
        self.place_calls += 1
        raise BrokerTimeout("ack lost")

    async def find_order_id(self, client_order_id: str) -> str | None:
        return "V-DEDUP" if self.landed else None

    async def cancel(self, client_order_id: str) -> None:
        raise NotImplementedError

    async def modify(self, client_order_id: str, **kwargs: object) -> None:
        raise NotImplementedError

    async def get_positions(self) -> list[Position]:
        return []

    async def get_orders(self) -> list[Order]:
        return []

    def subscribe_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        raise NotImplementedError

    def order_events(self) -> AsyncIterator[BrokerOrderEvent]:
        raise NotImplementedError


async def test_lost_ack_is_deduped_not_duplicated() -> None:
    # Transient failure but the venue actually has the order -> adopt its
    # venue_order_id instead of duplicating; order reaches OPEN.
    adapter = _LostAckBroker(landed=True)
    store = StateStore("sqlite:///:memory:")
    store.create_schema()
    oms = OMS(adapter=adapter, risk=_risk(), store=store, venue=Venue.NSE)
    order = await oms.submit_signal(_signal(), reference_price=Decimal("101"))
    assert order is not None
    assert order.state is OrderState.OPEN
    assert order.venue_order_id == "V-DEDUP"
    assert adapter.place_calls == 1  # placed once, never retried


async def test_truly_failed_placement_propagates() -> None:
    # Transient failure and the venue has nothing -> re-raise; the order stays
    # PENDING for reconcile to resolve (never a silent duplicate).
    adapter = _LostAckBroker(landed=False)
    store = StateStore("sqlite:///:memory:")
    store.create_schema()
    oms = OMS(adapter=adapter, risk=_risk(), store=store, venue=Venue.NSE)
    with pytest.raises(BrokerTimeout):
        await oms.submit_signal(_signal(), reference_price=Decimal("101"))
    with store.transaction() as s:
        orders = store.load_orders(s)
    assert orders[0].state is OrderState.PENDING  # persisted, awaiting reconcile


async def test_rebuild_state_from_fills() -> None:
    # P13.8: a fresh OMS on the same store reconstructs positions purely from the
    # append-only fills (the positions cache is never consulted).
    oms, _broker, store = _setup()
    await oms.submit_signal(_signal(qty="10"), reference_price=Decimal("101"))
    await oms.drain_events()
    await oms.submit_signal(_signal(qty="5"), reference_price=Decimal("101"))  # add 5
    await oms.drain_events()
    expected_qty = oms.positions[0].quantity  # 15

    # fresh process on the same DB — empty in-memory books
    fresh = OMS(adapter=_broker, risk=_risk(), store=store, venue=Venue.NSE)
    assert fresh.positions == []
    fresh.rebuild_state()
    rebuilt = [p for p in fresh.positions if p.quantity != 0]
    assert len(rebuilt) == 1
    assert rebuilt[0].symbol == SYMBOL
    assert rebuilt[0].quantity == expected_qty == Decimal("15")
    assert rebuilt[0].average_price is not None  # avg reconstructed, not just qty


async def test_rebuild_state_dedups_replayed_fills() -> None:
    # After a rebuild, the dedup set is populated so a duplicate fill is ignored.
    oms, broker, store = _setup()
    await oms.submit_signal(_signal(qty="10"), reference_price=Decimal("101"))
    await oms.drain_events()
    fresh = OMS(adapter=broker, risk=_risk(), store=store, venue=Venue.NSE)
    fresh.rebuild_state()
    assert len(fresh._seen_fills) >= 1


async def test_kill_on_flat_book_persists_halt_for_restart() -> None:
    # M1: a kill on a FLAT/position-less book flattens nothing, so it produces no fill —
    # and the daily-P&L row (otherwise written only on a fill) was never written, losing
    # the latched halt on restart. handle_kill now persists it regardless of fills.
    from alpha_core.execution.session import handle_kill

    oms, broker, store = _setup()
    assert oms.positions == []  # flat: the flatten below will produce no fill
    oms._risk.trip(KillTrigger.DAILY_LOSS)
    await handle_kill(oms, KillTrigger.DAILY_LOSS)
    td = OMS._trading_date(oms.now())
    with store.transaction() as s:
        row = store.load_daily_pnl(s, td)
        # the kill DECISION is audited (CLAUDE.md: audit every decision), with the chain intact
        kill_rows = [
            json.loads(r.payload)
            for r in s.execute(select(AuditRow)).scalars()
            if r.event_type == "KILL_SWITCH"
        ]
        assert store.verify_audit_chain(s) is True
    assert row is not None and row.halted is True and row.halt_trigger == "daily_loss"
    assert len(kill_rows) == 1 and kill_rows[0]["trigger"] == "daily_loss"
    # a fresh process over the same store restores the latch (no silent resume)
    fresh_risk = _risk()
    fresh = OMS(adapter=broker, risk=fresh_risk, store=store, venue=Venue.NSE)
    assert fresh_risk.is_halted is False
    fresh.restore_daily_state(td)
    assert fresh_risk.is_halted is True
    assert fresh_risk.halt_trigger is KillTrigger.DAILY_LOSS


async def test_restart_restores_latched_halt() -> None:
    store = StateStore("sqlite:///:memory:")
    store.create_schema()
    td = "2026-06-15"
    with store.transaction() as s:
        store.upsert_daily_pnl(
            s,
            trading_date=td,
            day_start_equity=Decimal("100000"),
            realized=Decimal("-2500"),
            unrealized=Decimal("0"),
            halted=True,
            halt_trigger="daily_loss",
            updated_at=datetime(2026, 6, 15, tzinfo=UTC),
        )
    # fresh process: new risk + OMS over the same store
    risk = _risk()
    broker = PaperBroker(
        cost_model=CostModel(COST_CONFIG),
        instruments={SYMBOL: InstrumentMeta(asset_class=AssetClass.EQUITY)},
        starting_cash=Decimal("1000000"),
    )
    oms = OMS(adapter=broker, risk=risk, store=store, venue=Venue.NSE)
    assert risk.is_halted is False
    oms.restore_daily_state(td)
    assert risk.is_halted is True  # latched halt survives restart
    assert risk.halt_trigger is KillTrigger.DAILY_LOSS


# --- G23: OMS time comes from the injected clock (backtest≡live) ----------------


async def test_oms_stamps_orders_from_injected_clock() -> None:
    clk = FakeClock(T0)
    oms, _broker, _store = _setup(clock=clk)
    order = await oms.submit_signal(_signal(), reference_price=Decimal("101"))
    assert order is not None
    # created/updated stamps are the clock instant, not wall-clock
    assert order.created_at == T0
    assert order.updated_at == T0
    assert oms.now() == T0


async def test_throttle_keys_off_clock_not_wall_clock() -> None:
    # The order throttle must measure its window in CLOCK time, so a backtest is
    # deterministic regardless of how fast it runs (G23). With a 2/min cap: two
    # orders at T0 pass, the third is throttled — but after the clock advances
    # past the minute window, a further order is allowed again.
    clk = FakeClock(T0)
    oms, _broker, _store = _setup(risk=_risk(max_orders_per_minute=2), clock=clk)
    assert await oms.submit_signal(_signal(), reference_price=Decimal("101")) is not None
    assert await oms.submit_signal(_signal(), reference_price=Decimal("101")) is not None
    # third within the same clock-minute -> throttled
    assert await oms.submit_signal(_signal(), reference_price=Decimal("101")) is None
    clk.advance(timedelta(seconds=61))  # bar time moves past the window
    assert await oms.submit_signal(_signal(), reference_price=Decimal("101")) is not None


# --- ADR 0014: in-flight exposure reservation (the acceptance scenario) ---------


class _ControlledBroker(BrokerAdapter):
    """ACKs placements but never auto-fills; emits only events you queue, so an
    order can be held OPEN (in-flight) and then cancelled deterministically."""

    def __init__(self) -> None:
        self._events: list[BrokerOrderEvent] = []

    async def place_order(self, order: Order) -> str:
        return f"V-{order.client_order_id}"

    async def find_order_id(self, client_order_id: str) -> str | None:
        return None

    async def cancel(self, client_order_id: str) -> None:
        self._events.append(
            BrokerOrderEvent(kind=BrokerEventKind.CANCEL, client_order_id=client_order_id)
        )

    async def modify(self, client_order_id: str, **kwargs: object) -> None:
        raise NotImplementedError

    async def get_positions(self) -> list[Position]:
        return []

    async def get_orders(self) -> list[Order]:
        return []

    def subscribe_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        raise NotImplementedError

    async def order_events(self) -> AsyncIterator[BrokerOrderEvent]:
        while self._events:
            yield self._events.pop(0)


def _buy(symbol: str, qty: str, strategy_id: str) -> Signal:
    return Signal(
        strategy_id=strategy_id,
        symbol=symbol,
        asset_class=AssetClass.EQUITY,
        side=Side.BUY,
        quantity=Decimal(qty),
        order_type=OrderType.MARKET,
        created_at=T0,
    )


async def test_inflight_reservation_blocks_then_releases_on_cancel() -> None:
    # ADR 0014 acceptance: Strategy A's in-flight buy takes the equity segment to
    # 90% of cap (54000 of 60000). Strategy B's buy that would breach is rejected
    # WHILE A is in-flight. When A's order cancels (reservation released), B's
    # resubmission is accepted. Segment is the only binding cap here.
    # Loosen every cap except the per-segment one; margin (12800 < capital) and
    # the other checks never bind, so the segment cap is the sole constraint.
    risk = _risk(
        max_position_per_instrument="10.00",
        max_gross_exposure="10.00",
        max_order_value="10.00",
        per_segment_exposure_cap="0.60",  # 60000 of 100000
    )
    adapter = _ControlledBroker()
    store = StateStore("sqlite:///:memory:")
    store.create_schema()
    oms = OMS(adapter=adapter, risk=risk, store=store, venue=Venue.NSE)

    # A: buy 540 @100 = 54000 -> OPEN, reserved (no fill drained).
    a = await oms.submit_signal(_buy("NSE:AAA", "540", "stratA"), reference_price=Decimal("100"))
    assert a is not None and a.state is OrderState.OPEN

    # B: buy 100 @100 = 10000 -> 54000 + 10000 = 64000 > 60000 -> rejected.
    b = await oms.submit_signal(_buy("NSE:BBB", "100", "stratB"), reference_price=Decimal("100"))
    assert b is None  # reservation of A's in-flight order blocks B

    # Cancel A -> reservation released.
    await oms.cancel_all_working()
    await oms.drain_events()
    assert oms._orders[a.client_order_id].state is OrderState.CANCELLED

    # B resubmitted -> now within the segment cap -> accepted.
    b2 = await oms.submit_signal(_buy("NSE:BBB", "100", "stratB"), reference_price=Decimal("100"))
    assert b2 is not None and b2.state is OrderState.OPEN


async def test_broker_rejection_releases_reservation() -> None:
    # A broker REJECT (here: a symbol the PaperBroker has no meta for) drives the
    # order terminal AND releases its in-flight reservation (ADR 0014 / P19.3).
    oms, _broker, _store = _setup()
    sig = Signal(
        strategy_id="s1",
        symbol="NSE:UNKNOWN",  # not in the broker's instruments -> OrderRejected
        asset_class=AssetClass.EQUITY,
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
        created_at=T0,
    )
    order = await oms.submit_signal(sig, reference_price=Decimal("100"))
    assert order is not None and order.state is OrderState.REJECTED
    assert oms._working_exposure() == []  # reservation released, not left dangling


# --- reconcile adoption + state restore (F6 / EXEC-1/2/3) ----------------------


def _adopt_order(
    cid: str,
    state: OrderState,
    *,
    filled: str = "0",
    order_type: OrderType = OrderType.MARKET,
    limit_price: Decimal | None = None,
) -> Order:
    fq = Decimal(filled)
    return Order(
        client_order_id=cid,
        symbol=SYMBOL,
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        side=Side.BUY,
        order_type=order_type,
        quantity=Decimal("10"),
        limit_price=limit_price,
        state=state,
        strategy_id="s1",
        venue_order_id="V1",
        filled_quantity=fq,
        average_fill_price=Decimal("100") if fq > 0 else None,
        created_at=T0,
        updated_at=T0,
    )


def _adopt_pos(qty: str) -> Position:
    return Position(
        venue=Venue.NSE,
        symbol=SYMBOL,
        asset_class=AssetClass.EQUITY,
        quantity=Decimal(qty),
        average_price=Decimal("100"),
        realized_pnl=Decimal("0"),
        last_price=Decimal("100"),
        updated_at=T0,
    )


async def test_apply_reconciliation_books_orders_and_positions() -> None:
    # F6: the reconciler computes adoptions; apply_reconciliation is the consumer
    # that books them into the local book AND persists them with an audit row.
    oms, _broker, store = _setup()
    await oms.apply_reconciliation(
        adopted_orders=[_adopt_order("o1", OrderState.FILLED, filled="10")],
        adopted_positions=[_adopt_pos("10")],
    )
    assert oms.orders[0].state is OrderState.FILLED
    assert {(p.venue, p.symbol): p.quantity for p in oms.positions} == {
        (Venue.NSE, SYMBOL): Decimal("10")
    }
    with store.transaction() as s:
        persisted_orders = {o.client_order_id: o.state for o in store.load_orders(s)}
        persisted_pos = {(p.venue, p.symbol): p.quantity for p in store.load_positions(s)}
        events = [r.event_type for r in s.execute(select(AuditRow)).scalars()]
    assert persisted_orders["o1"] is OrderState.FILLED  # durable, not just in-memory
    assert persisted_pos[(Venue.NSE, SYMBOL)] == Decimal("10")
    assert "RECONCILE_ADOPT_ORDER" in events and "RECONCILE_ADOPT_POSITION" in events


async def test_audit_reconcile_halt_persists_issues() -> None:
    # EXEC-2: a reconcile mismatch must leave a durable, hash-chained audit row with
    # the issues — not just an ephemeral log line — so a post-mortem can read why.
    oms, _broker, store = _setup()
    issues = ["position mismatch RELIANCE: broker 10 vs local 0"]
    await oms.audit_reconcile_halt(issues)
    with store.transaction() as s:
        halt = [
            json.loads(r.payload)
            for r in s.execute(select(AuditRow)).scalars()
            if r.event_type == "RECONCILE_HALT"
        ]
        assert store.verify_audit_chain(s) is True  # still tamper-evident
    assert len(halt) == 1
    assert halt[0]["issues"] == issues


async def test_apply_reconciliation_noop_when_nothing_adopted() -> None:
    oms, _broker, store = _setup()
    await oms.apply_reconciliation(adopted_orders=[], adopted_positions=[])
    assert oms.orders == [] and oms.positions == []
    with store.transaction() as s:
        assert store.load_orders(s) == []


def test_rebuild_state_restores_resting_orders_only() -> None:
    # EXEC-3: a cold start must restore resting orders so the startup reconcile can
    # diff them — terminal orders are not re-tracked.
    _oms, broker, store = _setup()
    with store.transaction() as s:
        store.save_order(
            s,
            _adopt_order(
                "resting", OrderState.OPEN, order_type=OrderType.LIMIT, limit_price=Decimal("100")
            ),
        )
        store.save_order(s, _adopt_order("done", OrderState.FILLED, filled="10"))
    fresh = OMS(adapter=broker, risk=_risk(), store=store, venue=Venue.NSE)
    fresh.rebuild_state()
    assert [o.client_order_id for o in fresh.orders] == ["resting"]
    assert fresh._working_price["resting"] == Decimal("100")  # limit price restored for reservation


def test_rebuild_state_seeds_dedup_with_fsm_fill_key() -> None:
    # S3: rebuild_state must seed the dedup set with the SAME key the FSM dedups on
    # (venue_fill_id or fill_id). A fill carrying a venue_fill_id seeded by raw fill_id
    # would let a broker-redelivered fill bypass dedup after a restart.
    _oms, broker, store = _setup()
    fill = Fill(
        fill_id="F1",
        client_order_id="c1",
        venue_order_id="V1",
        venue_fill_id="VF1",
        symbol=SYMBOL,
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("100"),
        ts=T0,
    )
    with store.transaction() as s:
        store.append_fill(s, fill)
    fresh = OMS(adapter=broker, risk=_risk(), store=store, venue=Venue.NSE)
    fresh.rebuild_state()
    assert "VF1" in fresh._seen_fills  # the FSM dedup key (venue_fill_id), not "F1"
