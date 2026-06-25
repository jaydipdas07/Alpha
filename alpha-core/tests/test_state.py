"""State store tests (ADR 0005)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from alpha_core.core.enums import AssetClass, OrderState, OrderType, Side, Venue
from alpha_core.core.models import Fill, Order, Position
from alpha_core.execution.state import StateStore

T0 = datetime(2026, 6, 15, 4, 0, tzinfo=UTC)


def _store() -> StateStore:
    s = StateStore("sqlite:///:memory:")
    s.create_schema()
    return s


def _order(state: OrderState = OrderState.NEW, filled: str = "0") -> Order:
    avg = Decimal("100") if Decimal(filled) > 0 else None
    return Order(
        client_order_id="alpha-1",
        symbol="NSE:RELIANCE",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("10"),
        state=state,
        filled_quantity=Decimal(filled),
        average_fill_price=avg,
        strategy_id="s1",
        created_at=T0,
        updated_at=T0,
    )


def _fill(fid: str, vfid: str, qty: str = "5", side: Side = Side.BUY) -> Fill:
    return Fill(
        fill_id=fid,
        client_order_id="alpha-1",
        venue_fill_id=vfid,
        symbol="NSE:RELIANCE",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        side=side,
        quantity=Decimal(qty),
        price=Decimal("100"),
        fees=Decimal("1"),
        ts=T0,
    )


# --- orders: decimal + tz round-trip, mutable update ---------------------------


def test_order_roundtrip_decimal_and_tz() -> None:
    store = _store()
    with store.transaction() as s:
        store.save_order(s, _order())
    with store.transaction() as s:
        loaded = store.load_orders(s)
    assert len(loaded) == 1
    assert loaded[0].quantity == Decimal("10")
    assert isinstance(loaded[0].quantity, Decimal)
    assert loaded[0].created_at == T0
    assert loaded[0].created_at.tzinfo is not None


def test_order_update_in_place() -> None:
    store = _store()
    with store.transaction() as s:
        store.save_order(s, _order())
    with store.transaction() as s:
        store.save_order(s, _order(state=OrderState.FILLED, filled="10"))
    with store.transaction() as s:
        loaded = store.load_orders(s)
    assert len(loaded) == 1  # same PK -> updated, not duplicated
    assert loaded[0].state is OrderState.FILLED


# --- fills: append-only + dedup ------------------------------------------------


def test_fill_dedup_by_venue_fill_id() -> None:
    store = _store()
    with store.transaction() as s:
        store.save_order(s, _order())
        store.append_fill(s, _fill("f1", "vf1"))
    with pytest.raises(IntegrityError), store.transaction() as s:
        store.append_fill(s, _fill("f2", "vf1"))  # duplicate venue_fill_id


def test_fills_append_only_no_update() -> None:
    store = _store()
    with store.transaction() as s:
        store.save_order(s, _order())
        store.append_fill(s, _fill("f1", "vf1"))
    with pytest.raises(IntegrityError), store.transaction() as s:
        s.execute(text("UPDATE fills SET price='999' WHERE fill_id='f1'"))


# --- audit: append-only + hash chain -------------------------------------------


def test_audit_append_only_no_delete() -> None:
    store = _store()
    with store.transaction() as s:
        store.append_audit(s, event_type="ORDER_SUBMIT", payload={"id": "alpha-1"})
    with pytest.raises(IntegrityError), store.transaction() as s:
        s.execute(text("DELETE FROM audit_log"))


def test_audit_hash_chain_verifies() -> None:
    store = _store()
    with store.transaction() as s:
        store.append_audit(s, event_type="A", payload={"n": 1})
        store.append_audit(s, event_type="B", payload={"n": 2})
        store.append_audit(s, event_type="C", payload={"n": 3})
    with store.transaction() as s:
        assert store.verify_audit_chain(s) is True


def test_audit_tamper_breaks_chain() -> None:
    store = _store()
    with store.transaction() as s:
        store.append_audit(s, event_type="A", payload={"n": 1})
        store.append_audit(s, event_type="B", payload={"n": 2})
    # Tamper at the raw DB level (triggers block UPDATE, so drop+reinsert a row's hash
    # via a fresh table is overkill; instead verify a doctored payload fails the walk).
    with store.transaction() as s:
        s.execute(text("DROP TRIGGER audit_log_no_update"))
        s.execute(text("UPDATE audit_log SET payload='{\"n\": 99}' WHERE event_type='A'"))
    with store.transaction() as s:
        assert store.verify_audit_chain(s) is False


# --- reconstruction ------------------------------------------------------------


def test_rebuild_positions_from_fills() -> None:
    store = _store()
    with store.transaction() as s:
        store.save_order(s, _order())
        store.append_fill(s, _fill("f1", "vf1", qty="5", side=Side.BUY))
        store.append_fill(s, _fill("f2", "vf2", qty="3", side=Side.BUY))
        store.append_fill(s, _fill("f3", "vf3", qty="2", side=Side.SELL))
    with store.transaction() as s:
        rebuilt = store.rebuild_positions(s)
    assert rebuilt[(Venue.NSE, "NSE:RELIANCE")] == Decimal("6")  # 5 + 3 - 2


# --- transaction atomicity -----------------------------------------------------


def test_transaction_rolls_back_on_error() -> None:
    store = _store()
    with pytest.raises(RuntimeError), store.transaction() as s:
        store.save_order(s, _order())
        raise RuntimeError("boom")  # should roll back the order insert
    with store.transaction() as s:
        assert store.load_orders(s) == []


# --- positions -----------------------------------------------------------------


def test_position_upsert() -> None:
    store = _store()
    pos = Position(
        venue=Venue.NSE,
        symbol="NSE:RELIANCE",
        asset_class=AssetClass.EQUITY,
        quantity=Decimal("10"),
        average_price=Decimal("100"),
        realized_pnl=Decimal("0"),
        updated_at=T0,
    )
    with store.transaction() as s:
        store.upsert_position(s, pos)
    with store.transaction() as s:
        loaded = store.load_positions(s)
    assert loaded[0].quantity == Decimal("10")
    assert loaded[0].average_price == Decimal("100")


# --- pnl_ledger + daily_pnl (H1) ----------------------------------------------


def test_pnl_ledger_and_daily_pnl() -> None:
    store = _store()
    with store.transaction() as s:
        store.append_pnl(
            s, ts=T0, venue=Venue.NSE, symbol="X", fill_id="f1", realized=Decimal("40")
        )
        store.upsert_daily_pnl(
            s,
            trading_date="2026-06-15",
            day_start_equity=Decimal("100000"),
            realized=Decimal("40"),
            unrealized=Decimal("0"),
            halted=False,
            halt_trigger=None,
            updated_at=T0,
        )
    with store.transaction() as s:
        row = store.load_daily_pnl(s, "2026-06-15")
    assert row is not None
    assert row.realized_pnl == Decimal("40")
    assert row.halted is False


def test_daily_pnl_upsert_overwrites() -> None:
    store = _store()
    for realized, halted in ((Decimal("40"), False), (Decimal("-2500"), True)):
        with store.transaction() as s:
            store.upsert_daily_pnl(
                s,
                trading_date="2026-06-15",
                day_start_equity=Decimal("100000"),
                realized=realized,
                unrealized=Decimal("0"),
                halted=halted,
                halt_trigger="daily_loss" if halted else None,
                updated_at=T0,
            )
    with store.transaction() as s:
        row = store.load_daily_pnl(s, "2026-06-15")
    assert row is not None
    assert row.realized_pnl == Decimal("-2500")
    assert row.halted is True


def test_pnl_ledger_append_only() -> None:
    store = _store()
    with store.transaction() as s:
        store.append_pnl(
            s, ts=T0, venue=Venue.NSE, symbol="X", fill_id="f1", realized=Decimal("40")
        )
    with pytest.raises(IntegrityError), store.transaction() as s:
        s.execute(text("UPDATE pnl_ledger SET realized_pnl='0'"))
