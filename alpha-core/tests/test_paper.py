"""PaperBroker tests (Phase 3 B3.4) — the golden harness."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from alpha_core.adapters.paper import PaperBroker
from alpha_core.core.enums import AssetClass, OrderState, OrderType, Side, Venue
from alpha_core.core.errors import OrderRejected, UnknownOrder
from alpha_core.core.interfaces import BrokerEventKind
from alpha_core.core.models import Order, Tick
from alpha_core.data.feed import ReplayFeed
from alpha_core.execution.costs import CostModel, InstrumentMeta

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
        "equity_intraday": {
            "brokerage": {"pct": 0.0003, "flat": 20, "mode": "min"},
            "stt": {"pct": 0.00025, "side": "sell"},
            "exchange_txn": {"pct": 0.0000297},
            "sebi": {"pct": 0.000001},
            "gst": {"pct": 0.18, "on": ["brokerage", "exchange_txn", "sebi"]},
            "stamp_duty": {"pct": 0.00003, "side": "buy"},
        },
        "index_option": {"brokerage": {"flat": 20, "mode": "flat"}},
        "crypto_perp": {"trading_fee": {"pct": 0.001, "side": "both"}},
    },
}
INSTRUMENTS = {SYMBOL: InstrumentMeta(asset_class=AssetClass.EQUITY)}


def _broker(cash: str = "1000000", feed: ReplayFeed | None = None) -> PaperBroker:
    return PaperBroker(
        cost_model=CostModel(COST_CONFIG),
        instruments=INSTRUMENTS,
        starting_cash=Decimal(cash),
        feed=feed,
    )


def _tick(bid: str = "99", ask: str = "101", ts: datetime = T0) -> Tick:
    return Tick(
        symbol=SYMBOL,
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        ts=ts,
        bid=Decimal(bid),
        ask=Decimal(ask),
        last_price=Decimal("100"),
    )


def _order(
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.MARKET,
    qty: str = "10",
    limit: str | None = None,
    cid: str = "alpha-1",
) -> Order:
    return Order(
        client_order_id=cid,
        symbol=SYMBOL,
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        side=side,
        order_type=order_type,
        quantity=Decimal(qty),
        limit_price=Decimal(limit) if limit else None,
        state=OrderState.NEW,
        strategy_id="s1",
        created_at=T0,
        updated_at=T0,
    )


# --- the headline: an order returns a simulated fill ---------------------------


async def test_market_order_fills() -> None:
    b = _broker()
    b.on_tick(_tick())
    vid = await b.place_order(_order())
    assert vid.startswith("P")
    fills = [e for e in b.emitted if e.kind is BrokerEventKind.FILL]
    assert len(fills) == 1
    assert fills[0].fill is not None
    assert fills[0].fill.quantity == Decimal("10")
    # buy fills at ask + 5bps slippage
    assert fills[0].fill.price == Decimal("101.0505")


async def test_fill_updates_position_and_cash() -> None:
    b = _broker(cash="1000000")
    b.on_tick(_tick())
    await b.place_order(_order())
    positions = await b.get_positions()
    assert len(positions) == 1
    assert positions[0].quantity == Decimal("10")
    assert b.cash < Decimal("1000000")  # paid for the buy + fees


async def test_order_events_stream() -> None:
    b = _broker()
    b.on_tick(_tick())
    await b.place_order(_order())
    events = [e async for e in b.order_events()]
    assert any(e.kind is BrokerEventKind.FILL for e in events)


# --- idempotency ---------------------------------------------------------------


async def test_idempotent_place() -> None:
    b = _broker()
    b.on_tick(_tick())
    v1 = await b.place_order(_order(cid="dup"))
    v2 = await b.place_order(_order(cid="dup"))
    assert v1 == v2
    assert len([e for e in b.emitted if e.kind is BrokerEventKind.FILL]) == 1


# --- market with no quote rejected ---------------------------------------------


async def test_market_without_quote_rejected() -> None:
    b = _broker()
    with pytest.raises(OrderRejected):
        await b.place_order(_order())


async def test_rejected_market_leaves_no_dedup_mapping() -> None:
    """A rejected (non-marketable) MARKET order must not register a client->venue
    id; otherwise a later dedup-by-query would treat it as placed (F5)."""
    b = _broker()
    with pytest.raises(OrderRejected):
        await b.place_order(_order(cid="x"))
    assert await b.find_order_id("x") is None  # no stale mapping
    # the same intent now succeeds once a quote exists (not deduped to a ghost id)
    b.on_tick(_tick())
    vid = await b.place_order(_order(cid="x"))
    assert vid is not None
    assert await b.find_order_id("x") == vid


# --- limit orders rest then fill -----------------------------------------------


async def test_limit_rests_until_marketable() -> None:
    b = _broker()
    b.on_tick(_tick(bid="99", ask="101"))
    # buy limit 100 is not marketable (ask 101 > 100) -> rests
    await b.place_order(_order(order_type=OrderType.LIMIT, limit="100", cid="lim"))
    assert not [e for e in b.emitted if e.kind is BrokerEventKind.FILL]
    assert len(await b.get_orders()) == 1
    # ask drops to 100 -> now marketable -> fills
    b.on_tick(_tick(bid="98", ask="100"))
    assert [e for e in b.emitted if e.kind is BrokerEventKind.FILL]
    # The filled order STAYS visible in its terminal state (the adapter contract —
    # get_orders is the whole session, not just the resting book; #159 BLOCKER).
    (snapshot,) = await b.get_orders()
    assert snapshot.state is OrderState.FILLED


# --- cancel --------------------------------------------------------------------


async def test_cancel_resting_emits_event() -> None:
    b = _broker()
    b.on_tick(_tick())
    await b.place_order(_order(order_type=OrderType.LIMIT, limit="50", cid="c1"))
    await b.cancel("c1")
    assert any(e.kind is BrokerEventKind.CANCEL for e in b.emitted)


async def test_cancel_unknown_raises() -> None:
    b = _broker()
    with pytest.raises(UnknownOrder):
        await b.cancel("nope")


# --- sell realizes P&L ---------------------------------------------------------


async def test_sell_realizes_pnl() -> None:
    b = _broker()
    b.on_tick(_tick(bid="99", ask="101"))
    await b.place_order(_order(side=Side.BUY, cid="buy"))  # avg ~101.0505
    b.on_tick(_tick(bid="109", ask="111"))
    await b.place_order(_order(side=Side.SELL, cid="sell"))  # sell at bid - slippage
    positions = await b.get_positions()
    assert positions[0].quantity == Decimal("0")
    assert positions[0].realized_pnl > 0  # bought ~101, sold ~109


# --- subscribe_ticks drives fills ----------------------------------------------


async def test_subscribe_ticks_updates_market() -> None:
    feed = ReplayFeed(ticks=[_tick(bid="98", ask="100", ts=T0)])
    b = _broker(feed=feed)
    await b.place_order(
        _order(order_type=OrderType.LIMIT, limit="100", cid="r1")
    )  # rests (no quote)
    received = [t async for t in b.subscribe_ticks([SYMBOL])]
    assert len(received) == 1
    # the tick made the limit marketable and filled it
    assert [e for e in b.emitted if e.kind is BrokerEventKind.FILL]


# --- modify / cancel / getters / no-feed (coverage) ----------------------------


async def test_modify_makes_resting_limit_marketable() -> None:
    b = _broker()
    b.on_tick(_tick(bid="99", ask="101"))
    await b.place_order(_order(order_type=OrderType.LIMIT, limit="90", cid="m1"))  # rests
    assert len([e for e in b.emitted if e.kind is BrokerEventKind.FILL]) == 0
    await b.modify("m1", limit_price=Decimal("105"))  # now marketable -> fills
    assert len([e for e in b.emitted if e.kind is BrokerEventKind.FILL]) == 1


async def test_modify_unknown_order_raises() -> None:
    with pytest.raises(UnknownOrder):
        await _broker().modify("nope", limit_price=Decimal("1"))


async def test_cancel_unknown_order_raises() -> None:
    with pytest.raises(UnknownOrder):
        await _broker().cancel("nope")


async def test_find_order_id_and_get_orders() -> None:
    b = _broker()
    b.on_tick(_tick())
    await b.place_order(_order(order_type=OrderType.LIMIT, limit="90", cid="r1"))  # rests
    assert await b.find_order_id("r1") is not None
    assert await b.find_order_id("nope") is None
    assert [o.client_order_id for o in await b.get_orders()] == ["r1"]


async def test_sell_limit_marketable_against_bid() -> None:
    b = _broker()
    b.on_tick(_tick(bid="101", ask="102"))
    await b.place_order(_order(side=Side.SELL, order_type=OrderType.LIMIT, limit="100", cid="s1"))
    assert len([e for e in b.emitted if e.kind is BrokerEventKind.FILL]) == 1  # bid >= limit


async def test_subscribe_ticks_without_feed_raises() -> None:
    with pytest.raises(RuntimeError):
        async for _ in _broker().subscribe_ticks([SYMBOL]):
            pass


# --- paper-crypto: simulated fills priced off a streamed venue feed (#296) -----


async def test_paper_crypto_fills_resting_order_via_streamed_feed() -> None:
    # The core paper-crypto loop (TEST-2): a PaperBroker priced by a venue DataFeed
    # (here a ReplayFeed standing in for the live AdapterFeed) fills a resting crypto
    # LIMIT order when the streamed tick makes it marketable — no real order is sent.
    sym = "BTC/USDT"
    tick = Tick(
        symbol=sym,
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        ts=T0,
        last_price=Decimal("65000"),
        volume=Decimal("0.5"),
    )
    broker = PaperBroker(
        cost_model=CostModel(COST_CONFIG),
        instruments={sym: InstrumentMeta(asset_class=AssetClass.CRYPTO)},
        starting_cash=Decimal("100000"),
        feed=ReplayFeed(ticks=[tick]),
    )
    order = Order(
        client_order_id="c1",
        symbol=sym,
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.01"),  # fractional crypto qty
        limit_price=Decimal("65000"),
        state=OrderState.NEW,
        strategy_id="s",
        created_at=T0,
        updated_at=T0,
    )
    await broker.place_order(order)  # no quote yet -> rests
    async for _ in broker.subscribe_ticks([sym]):
        pass  # streaming the feed drives on_tick, which fills the resting order

    events = [e async for e in broker.order_events()]
    assert any(
        e.kind is BrokerEventKind.FILL and e.fill is not None and e.fill.symbol == sym
        for e in events
    )


# --- the session-order contract (PR #159 review BLOCKER) -------------------------


async def test_get_orders_reports_the_full_session_not_just_resting() -> None:
    # An instantly-filled market order must stay visible as FILLED (the adapter
    # contract) — otherwise the reconciler reads a vanished order + a moved book as
    # unexplained drift and latches a spurious halt mid-soak.
    broker = _broker()
    broker.on_tick(_tick())
    await broker.place_order(_order(cid="alpha-m1"))
    (snapshot,) = await broker.get_orders()
    assert snapshot.state is OrderState.FILLED
    assert snapshot.filled_quantity == Decimal("10")
    assert snapshot.average_fill_price is not None
    # A cancelled resting order stays visible too, in its terminal state.
    await broker.place_order(_order(order_type=OrderType.LIMIT, limit="90", cid="alpha-l1"))
    await broker.cancel("alpha-l1")
    states = {o.client_order_id: o.state for o in await broker.get_orders()}
    assert states == {"alpha-m1": OrderState.FILLED, "alpha-l1": OrderState.CANCELLED}


async def test_reconcile_race_adopts_the_instant_fill_instead_of_halting() -> None:
    # The reproduced #159 BLOCKER: reconcile fires between the venue fill and the
    # OMS drain. Broker truth (FILLED order + moved position) must EXPLAIN the local
    # OPEN order + flat book as a missed fill -> CLEAN adoption, never a halt.
    from alpha_core.execution.reconcile import Reconciler, ReconcileStatus
    from alpha_core.risk.limits import RiskConfig
    from alpha_core.risk.manager import RiskManager

    broker = _broker()
    broker.on_tick(_tick())
    local_open = _order(cid="alpha-race")  # what the OMS holds: submitted, not yet drained
    await broker.place_order(local_open)  # venue-side: filled instantly
    risk = RiskManager(
        RiskConfig.model_validate(
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
    )
    report = await Reconciler(adapter=broker, risk=risk).reconcile(
        local_orders=[local_open.model_copy(update={"state": OrderState.OPEN})],
        local_positions=[],
    )
    assert report.status is ReconcileStatus.CLEAN
    assert not risk.is_halted
    assert [o.state for o in report.adopted_orders] == [OrderState.FILLED]
    assert [p.quantity for p in report.adopted_positions] == [Decimal("10")]


async def test_seed_from_store_restores_broker_truth_across_restart() -> None:
    # S1: a restart while holding must NOT brick the startup gate — the fresh paper
    # book re-homes the durable store's positions/orders, so broker == local again.
    broker = _broker()
    broker.on_tick(_tick())
    await broker.place_order(_order(cid="alpha-s1"))
    held = (await broker.get_positions())[0]
    (filled,) = await broker.get_orders()

    fresh = _broker()  # the restarted process: empty book
    fresh.seed_from_store([filled], [held])
    assert (await fresh.get_positions())[0].quantity == held.quantity
    (seen,) = await fresh.get_orders()
    assert seen.state is OrderState.FILLED  # dedup/reconcile can see the old order
    assert await fresh.find_order_id("alpha-s1") is not None
    # And a still-working limit order rests again, fillable by a later tick.
    resting = _order(order_type=OrderType.LIMIT, limit="90", cid="alpha-s2").model_copy(
        update={"state": OrderState.OPEN}
    )
    fresh.seed_from_store([resting], [])
    fresh.on_tick(_tick(bid="88", ask="89"))  # crosses the 90 limit
    fills = [e for e in fresh.emitted if e.kind.name == "FILL"]
    assert any(e.client_order_id == "alpha-s2" for e in fills)
