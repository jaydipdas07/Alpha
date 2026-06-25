"""Property-based tests for position accounting (G13).

``apply_fill`` is the one place position math lives (OMS + paper venue), so its
invariants are worth proving over many random fill sequences, not just examples.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from alpha_core.core.enums import AssetClass, Side, Venue
from alpha_core.core.models import Fill, Position
from alpha_core.execution.positions import apply_fill

TS = datetime(2026, 6, 15, 4, 0, tzinfo=UTC)
_money = st.decimals(min_value=Decimal("0.05"), max_value=Decimal("100000"), places=2)
_qty = st.decimals(min_value=Decimal("1"), max_value=Decimal("10000"), places=0)


@st.composite
def _fill(draw: st.DrawFn) -> Fill:
    return Fill(
        fill_id=draw(st.uuids()).hex,
        client_order_id="alpha-x",
        symbol="NSE:RELIANCE",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        side=draw(st.sampled_from([Side.BUY, Side.SELL])),
        quantity=draw(_qty),
        price=draw(_money),
        ts=TS,
    )


def _signed(f: Fill) -> Decimal:
    return f.quantity if f.side is Side.BUY else -f.quantity


@given(fills=st.lists(_fill(), min_size=1, max_size=25))
def test_net_quantity_is_sum_of_signed_fills(fills: list[Fill]) -> None:
    pos: Position | None = None
    for f in fills:
        pos = apply_fill(pos, f)
    assert pos is not None
    assert pos.quantity == sum((_signed(f) for f in fills), Decimal(0))


@given(fills=st.lists(_fill(), min_size=1, max_size=25))
def test_average_price_set_iff_open(fills: list[Fill]) -> None:
    pos: Position | None = None
    for f in fills:
        pos = apply_fill(pos, f)
    assert pos is not None
    # the Position invariant must hold after every applied fill sequence
    assert (pos.quantity == 0) == (pos.average_price is None)


@given(qty=_qty, buy_price=_money, sell_price=_money)
def test_round_trip_realizes_price_difference(
    qty: Decimal, buy_price: Decimal, sell_price: Decimal
) -> None:
    buy = Fill(
        fill_id="b",
        client_order_id="alpha-x",
        symbol="NSE:RELIANCE",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        side=Side.BUY,
        quantity=qty,
        price=buy_price,
        ts=TS,
    )
    sell = buy.model_copy(update={"fill_id": "s", "side": Side.SELL, "price": sell_price})
    pos = apply_fill(apply_fill(None, buy), sell)
    assert pos.quantity == 0
    assert pos.average_price is None
    assert pos.realized_pnl == (sell_price - buy_price) * qty  # long P&L = (exit-entry)*qty
