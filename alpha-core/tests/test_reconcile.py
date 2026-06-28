"""Reconciliation tests (ADR 0009)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from alpha_core.core.enums import AssetClass, OrderState, OrderType, Side, Venue
from alpha_core.core.interfaces import BrokerAdapter, BrokerOrderEvent
from alpha_core.core.models import Order, Position, Tick
from alpha_core.execution.oms import OMS
from alpha_core.execution.reconcile import Reconciler, ReconcileStatus
from alpha_core.execution.state import StateStore
from alpha_core.risk.limits import RiskConfig
from alpha_core.risk.manager import KillTrigger, RiskManager

T0 = datetime(2026, 6, 15, 4, 0, tzinfo=UTC)


def _risk() -> RiskManager:
    cfg = RiskConfig.model_validate(
        {
            "base_capital": "100000",
            "limits": {
                "max_gross_exposure": "1.00",
                "max_position_per_instrument": "0.20",
                "max_concurrent_positions": 5,
                "max_order_value": "0.25",
                "max_orders_per_minute": 10,
                "max_daily_loss_halt": "0.02",
                "max_loss_per_trade": "0.01",
                "per_segment_exposure_cap": "0.60",
            },
        }
    )
    return RiskManager(cfg)


def _pos(symbol: str, qty: str) -> Position:
    return Position(
        venue=Venue.NSE,
        symbol=symbol,
        asset_class=AssetClass.EQUITY,
        quantity=Decimal(qty),
        average_price=Decimal("100"),
        realized_pnl=Decimal("0"),
        last_price=Decimal("100"),
        updated_at=T0,
    )


def _order(
    cid: str,
    state: OrderState,
    *,
    venue_order_id: str | None = None,
    filled: str = "0",
) -> Order:
    filled_qty = Decimal(filled)
    return Order(
        client_order_id=cid,
        symbol="NSE:RELIANCE",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("10"),
        state=state,
        strategy_id="s1",
        venue_order_id=venue_order_id,
        filled_quantity=filled_qty,
        average_fill_price=Decimal("100") if filled_qty > 0 else None,
        created_at=T0,
        updated_at=T0,
    )


class _StubAdapter(BrokerAdapter):
    """Returns controlled broker truth; other methods are unused here."""

    def __init__(self, positions: list[Position], orders: list[Order]) -> None:
        self._positions = positions
        self._orders = orders

    async def place_order(self, order: Order) -> str:
        raise NotImplementedError

    async def cancel(self, client_order_id: str) -> None:
        raise NotImplementedError

    async def modify(self, client_order_id: str, **kwargs: object) -> None:
        raise NotImplementedError

    async def get_positions(self) -> list[Position]:
        return self._positions

    async def get_orders(self) -> list[Order]:
        return self._orders

    async def find_order_id(self, client_order_id: str) -> str | None:
        for o in self._orders:
            if o.client_order_id == client_order_id:
                return o.venue_order_id
        return None

    def subscribe_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        raise NotImplementedError

    def order_events(self) -> AsyncIterator[BrokerOrderEvent]:
        raise NotImplementedError


# --- clean ---------------------------------------------------------------------


async def test_clean_when_matching() -> None:
    risk = _risk()
    adapter = _StubAdapter([_pos("NSE:RELIANCE", "10")], [])
    rec = Reconciler(adapter=adapter, risk=risk)
    report = await rec.reconcile(
        local_orders=[_order("o1", OrderState.FILLED)],
        local_positions=[_pos("NSE:RELIANCE", "10")],
    )
    assert report.status is ReconcileStatus.CLEAN
    assert risk.is_halted is False


async def test_startup_gate_allows_when_clean() -> None:
    rec = Reconciler(adapter=_StubAdapter([], []), risk=_risk())
    assert await rec.startup_gate(local_orders=[], local_positions=[]) is True


# --- irreconcilable -> halt ----------------------------------------------------


async def test_position_mismatch_halts() -> None:
    risk = _risk()
    adapter = _StubAdapter([_pos("NSE:RELIANCE", "7")], [])  # broker says 7
    rec = Reconciler(adapter=adapter, risk=risk)
    report = await rec.reconcile(
        local_orders=[],
        local_positions=[_pos("NSE:RELIANCE", "10")],  # local says 10
    )
    assert report.status is ReconcileStatus.HALTED
    assert any("position mismatch" in i for i in report.issues)
    assert risk.is_halted is True
    assert risk.halt_trigger is KillTrigger.RECONCILIATION_MISMATCH


async def test_unknown_order_at_broker_halts() -> None:
    risk = _risk()
    adapter = _StubAdapter([], [_order("ghost", OrderState.OPEN)])
    rec = Reconciler(adapter=adapter, risk=risk)
    report = await rec.reconcile(local_orders=[], local_positions=[])
    assert report.status is ReconcileStatus.HALTED
    assert any("unknown order at broker" in i for i in report.issues)
    assert risk.is_halted is True


async def test_local_live_order_missing_at_broker_halts() -> None:
    risk = _risk()
    adapter = _StubAdapter([], [])  # broker has nothing
    rec = Reconciler(adapter=adapter, risk=risk)
    report = await rec.reconcile(local_orders=[_order("o1", OrderState.OPEN)], local_positions=[])
    assert report.status is ReconcileStatus.HALTED
    assert any("missing at broker" in i for i in report.issues)


async def test_terminal_local_order_not_flagged() -> None:
    # A filled local order absent from the broker's working set is fine.
    rec = Reconciler(adapter=_StubAdapter([], []), risk=_risk())
    report = await rec.reconcile(local_orders=[_order("o1", OrderState.FILLED)], local_positions=[])
    assert report.status is ReconcileStatus.CLEAN


async def test_startup_gate_blocks_when_mismatch() -> None:
    rec = Reconciler(adapter=_StubAdapter([_pos("X", "5")], []), risk=_risk())
    assert await rec.startup_gate(local_orders=[], local_positions=[]) is False


# --- recoverable -> adopt (H7) -------------------------------------------------


async def test_missed_ack_adopts_broker_order() -> None:
    # Local order is PENDING with no venue id; the broker already acked it.
    risk = _risk()
    broker_order = _order("o1", OrderState.OPEN, venue_order_id="V1")
    adapter = _StubAdapter([], [broker_order])
    rec = Reconciler(adapter=adapter, risk=risk)
    report = await rec.reconcile(
        local_orders=[_order("o1", OrderState.PENDING)], local_positions=[]
    )
    assert report.status is ReconcileStatus.CLEAN
    assert risk.is_halted is False
    assert report.adopted_orders == [broker_order]
    assert any("adopt missed ack" in r for r in report.recovered)


async def test_never_placed_order_cancelled_locally() -> None:
    # Local order is PENDING with no venue id and the broker has never heard of it.
    risk = _risk()
    rec = Reconciler(adapter=_StubAdapter([], []), risk=risk)
    report = await rec.reconcile(
        local_orders=[_order("o1", OrderState.PENDING)], local_positions=[]
    )
    assert report.status is ReconcileStatus.CLEAN
    assert risk.is_halted is False
    assert len(report.adopted_orders) == 1
    assert report.adopted_orders[0].state is OrderState.CANCELLED
    assert any("never placed" in r for r in report.recovered)


async def test_missed_fill_adopts_broker_truth() -> None:
    # Local order OPEN with 0 fills; broker shows it filled.
    risk = _risk()
    broker_order = _order("o1", OrderState.FILLED, venue_order_id="V1", filled="10")
    adapter = _StubAdapter([_pos("NSE:RELIANCE", "10")], [broker_order])
    rec = Reconciler(adapter=adapter, risk=risk)
    report = await rec.reconcile(
        local_orders=[_order("o1", OrderState.OPEN, venue_order_id="V1")],
        local_positions=[],  # local hasn't booked the fill yet
    )
    assert report.status is ReconcileStatus.CLEAN
    assert risk.is_halted is False
    assert report.adopted_orders == [broker_order]
    # the position drift is explained by the adopted fill -> adopt broker positions
    assert report.adopted_positions == [_pos("NSE:RELIANCE", "10")]
    assert any("adopt missed fill" in r for r in report.recovered)


async def test_unexplained_position_drift_still_halts() -> None:
    # Position mismatch with no order to explain it -> halt, never guess.
    risk = _risk()
    adapter = _StubAdapter([_pos("NSE:RELIANCE", "10")], [])
    rec = Reconciler(adapter=adapter, risk=risk)
    report = await rec.reconcile(local_orders=[], local_positions=[])
    assert report.status is ReconcileStatus.HALTED
    assert not report.adopted_positions
    assert risk.is_halted is True


async def test_reconcile_then_apply_books_broker_truth() -> None:
    # End-to-end F6: a missed fill reconciles to CLEAN-with-adoptions, and applying
    # the report makes the OMS book match broker truth — before the fix the report
    # was computed and silently dropped, leaving the local book known-divergent.
    store = StateStore("sqlite:///:memory:")
    store.create_schema()
    risk = _risk()
    broker_order = _order("o1", OrderState.FILLED, venue_order_id="V1", filled="10")
    adapter = _StubAdapter([_pos("NSE:RELIANCE", "10")], [broker_order])
    oms = OMS(adapter=adapter, risk=risk, store=store, venue=Venue.NSE)
    oms._orders["o1"] = _order("o1", OrderState.OPEN, venue_order_id="V1")  # local: no fill booked

    report = await Reconciler(adapter=adapter, risk=risk).reconcile(
        local_orders=oms.orders, local_positions=oms.positions
    )
    assert report.status is ReconcileStatus.CLEAN
    await oms.apply_reconciliation(
        adopted_orders=report.adopted_orders, adopted_positions=report.adopted_positions
    )
    assert oms.orders[0].state is OrderState.FILLED
    assert {(p.venue, p.symbol): p.quantity for p in oms.positions} == {
        (Venue.NSE, "NSE:RELIANCE"): Decimal("10")
    }


async def test_explained_drift_does_not_mask_unrelated_drift() -> None:
    # An adopted fill on RELIANCE explains RELIANCE's drift, but the broker also
    # shows an unexplained TCS position. The explained adoption must NOT let the
    # unrelated drift through -> halt on TCS (F4).
    risk = _risk()
    broker_order = _order("o1", OrderState.FILLED, venue_order_id="V1", filled="10")
    adapter = _StubAdapter(
        [_pos("NSE:RELIANCE", "10"), _pos("NSE:TCS", "5")],  # TCS is unexplained
        [broker_order],
    )
    rec = Reconciler(adapter=adapter, risk=risk)
    report = await rec.reconcile(
        local_orders=[_order("o1", OrderState.OPEN, venue_order_id="V1")],
        local_positions=[],
    )
    assert report.status is ReconcileStatus.HALTED
    assert any("NSE:TCS" in i for i in report.issues)
    assert not any("NSE:RELIANCE" in i for i in report.issues)  # RELIANCE was explained
    assert not report.adopted_positions  # halt -> no adoption applied
    assert risk.is_halted is True


async def test_reconcile_zeroes_position_the_broker_reports_flat() -> None:
    # M2: local still holds a position the broker reports FLAT (omits it). An adopted
    # order on the same symbol explains the mismatch, so reconcile is CLEAN — but broker
    # truth is "flat", and apply_reconciliation only SETS positions. Reconcile must emit
    # an explicit zero, else the stale local position survives.
    risk = _risk()
    broker_cancelled = _order("o1", OrderState.CANCELLED, venue_order_id="V1")  # terminal
    adapter = _StubAdapter([], [broker_cancelled])  # broker is FLAT (no positions)
    rec = Reconciler(adapter=adapter, risk=risk)
    report = await rec.reconcile(
        local_orders=[_order("o1", OrderState.OPEN, venue_order_id="V1")],
        local_positions=[_pos("NSE:RELIANCE", "10")],  # local still thinks it holds 10
    )
    assert report.status is ReconcileStatus.CLEAN
    flat = [p for p in report.adopted_positions if p.symbol == "NSE:RELIANCE"]
    assert len(flat) == 1 and flat[0].quantity == Decimal("0")


async def test_reconcile_locked_clean_adopts_under_the_lock() -> None:
    # reconcile_locked runs the reconcile + applies adoptions atomically under the OMS
    # lock (so a concurrent submit/fill can't make the snapshot stale -> spurious halt).
    store = StateStore("sqlite:///:memory:")
    store.create_schema()
    risk = _risk()
    broker_order = _order("o1", OrderState.FILLED, venue_order_id="V1", filled="10")
    adapter = _StubAdapter([_pos("NSE:RELIANCE", "10")], [broker_order])
    oms = OMS(adapter=adapter, risk=risk, store=store, venue=Venue.NSE)
    oms._orders["o1"] = _order("o1", OrderState.OPEN, venue_order_id="V1")  # missed the fill
    report = await oms.reconcile_locked(Reconciler(adapter=adapter, risk=risk))
    assert report.status is ReconcileStatus.CLEAN
    assert risk.is_halted is False
    assert oms.orders[0].state is OrderState.FILLED  # adopted
    assert {(p.venue, p.symbol): p.quantity for p in oms.positions} == {
        (Venue.NSE, "NSE:RELIANCE"): Decimal("10")
    }


async def test_reconcile_locked_dirty_halts() -> None:
    store = StateStore("sqlite:///:memory:")
    store.create_schema()
    risk = _risk()
    adapter = _StubAdapter([_pos("NSE:RELIANCE", "7")], [])  # broker 7
    oms = OMS(adapter=adapter, risk=risk, store=store, venue=Venue.NSE)
    oms._positions[(Venue.NSE, "NSE:RELIANCE")] = _pos("NSE:RELIANCE", "10")  # local 10 -> drift
    report = await oms.reconcile_locked(Reconciler(adapter=adapter, risk=risk))
    assert report.status is ReconcileStatus.HALTED
    assert risk.is_halted is True


async def test_apply_reconciliation_flattens_a_broker_flat_position() -> None:
    # End-to-end (M2): applying the CLEAN report actually flattens the phantom local
    # position to match broker truth (before the fix it stayed at the stale quantity).
    store = StateStore("sqlite:///:memory:")
    store.create_schema()
    risk = _risk()
    broker_cancelled = _order("o1", OrderState.CANCELLED, venue_order_id="V1")
    adapter = _StubAdapter([], [broker_cancelled])
    oms = OMS(adapter=adapter, risk=risk, store=store, venue=Venue.NSE)
    oms._orders["o1"] = _order("o1", OrderState.OPEN, venue_order_id="V1")
    oms._positions[(Venue.NSE, "NSE:RELIANCE")] = _pos("NSE:RELIANCE", "10")
    report = await Reconciler(adapter=adapter, risk=risk).reconcile(
        local_orders=oms.orders, local_positions=oms.positions
    )
    assert report.status is ReconcileStatus.CLEAN
    await oms.apply_reconciliation(
        adopted_orders=report.adopted_orders, adopted_positions=report.adopted_positions
    )
    assert all(p.quantity == Decimal("0") for p in oms.positions if p.symbol == "NSE:RELIANCE")
