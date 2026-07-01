"""Property-based tests for the cost model (P13.9).

Costs must be well-behaved: monotonic in size, never negative, and the 2x stress
estimate never cheaper than the base — a backtest relies on these holding for any
order, not just the few in the example tests.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from alpha_core.core.enums import AssetClass, Side
from alpha_core.execution.costs import CostModel, InstrumentMeta

_CFG: dict[str, object] = {
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
_MODEL = CostModel(_CFG)
_META = InstrumentMeta(asset_class=AssetClass.EQUITY)

_qty = st.decimals(min_value=Decimal("1"), max_value=Decimal("100000"), places=0)
_price = st.decimals(min_value=Decimal("1"), max_value=Decimal("100000"), places=2)


@given(q1=_qty, q2=_qty, price=_price)
def test_total_cost_monotonic_in_quantity(q1: Decimal, q2: Decimal, price: Decimal) -> None:
    lo, hi = sorted([q1, q2])
    c_lo = _MODEL.estimate(side=Side.BUY, quantity=lo, ltp=price, instrument=_META)
    c_hi = _MODEL.estimate(side=Side.BUY, quantity=hi, ltp=price, instrument=_META)
    assert c_hi.total >= c_lo.total


@given(q=_qty, price=_price)
def test_stress_never_cheaper_than_base(q: Decimal, price: Decimal) -> None:
    base = _MODEL.estimate(side=Side.BUY, quantity=q, ltp=price, instrument=_META, stress=False)
    stress = _MODEL.estimate(side=Side.BUY, quantity=q, ltp=price, instrument=_META, stress=True)
    assert stress.total >= base.total


@given(q=_qty, price=_price, buy=st.booleans())
def test_costs_never_negative(q: Decimal, price: Decimal, buy: bool) -> None:
    side = Side.BUY if buy else Side.SELL
    c = _MODEL.estimate(side=side, quantity=q, ltp=price, instrument=_META)
    for component in (
        c.spread_cost,
        c.slippage_cost,
        c.brokerage,
        c.stt,
        c.exchange_txn,
        c.gst,
        c.sebi,
        c.stamp_duty,
        c.tds,
        c.total,
    ):
        assert component >= 0
