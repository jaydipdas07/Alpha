"""Post-only fill model tests (the maker execution capability — Phase-4+ / the frozen-five
read instrument): GTX arrival rejection, strict trade-through resting fills AT the limit,
TTL expiry, reduce-only sizing, and the OHLC-tick tape."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alpha_core.adapters.paper import PaperBroker
from alpha_core.core.enums import AssetClass, OrderState, OrderType, Side, Venue
from alpha_core.core.errors import OrderRejected
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Order, Signal, Tick
from alpha_core.execution.costs import CostModel, InstrumentMeta
from alpha_core.execution.oms import OMS
from alpha_core.execution.state import StateStore
from alpha_core.risk.limits import RiskConfig
from alpha_core.risk.manager import RiskManager
from alpha_core.scheduler.clock import FakeClock

T0 = datetime(2026, 6, 15, 4, 0, tzinfo=UTC)
SYMBOL = "BTCUSDT"

# Maker-scenario-shaped config: zero spread synthesis + zero slippage, 2bps fee — a
# resting fill must land at EXACTLY the limit price plus fees on that notional.
MAKER_COSTS: dict[str, object] = {
    "slippage": {
        "crypto_perp": {"type": "bps", "value": 0},
        "default_spread": {"crypto_perp": 0},
        "stress_multiplier": 2,
    },
    "segments": {"crypto_perp": {"trading_fee": {"pct": 0.0002, "side": "both"}}},
}


def _tick(price: str, *, ts: datetime = T0, bid: str | None = None, ask: str | None = None) -> Tick:
    return Tick(
        symbol=SYMBOL,
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        ts=ts,
        last_price=Decimal(price),
        bid=Decimal(bid) if bid else None,
        ask=Decimal(ask) if ask else None,
    )


def _broker() -> PaperBroker:
    b = PaperBroker(
        cost_model=CostModel(MAKER_COSTS),
        instruments={SYMBOL: InstrumentMeta(asset_class=AssetClass.CRYPTO)},
        starting_cash=Decimal("1000000"),
    )
    b.on_tick(_tick("100"))
    return b


def _order(
    side: Side = Side.BUY,
    limit: str = "100",
    *,
    post_only: bool = True,
    valid_until: datetime | None = None,
    cid: str = "c1",
) -> Order:
    return Order(
        client_order_id=cid,
        symbol=SYMBOL,
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Decimal("2"),
        limit_price=Decimal(limit),
        post_only=post_only,
        valid_until=valid_until,
        state=OrderState.NEW,
        strategy_id="t",
        created_at=T0,
        updated_at=T0,
    )


async def test_gtx_rejects_a_crossing_arrival_with_a_real_book() -> None:
    broker = _broker()
    broker.on_tick(_tick("100", bid="99", ask="100"))  # ask == limit -> would take
    with pytest.raises(OrderRejected) as exc:
        await broker.place_order(_order(limit="100"))
    assert exc.value.reason == "post_only"


async def test_ltp_only_placement_at_the_print_rests() -> None:
    # The bar-granularity convention: placing AT the last print rests; only a print
    # strictly beyond the limit counts as would-cross on arrival.
    broker = _broker()  # ltp == 100
    await broker.place_order(_order(limit="100"))  # rests, no exception
    assert (await broker.get_orders())[0].state is OrderState.OPEN
    broker2 = _broker()
    broker2.on_tick(_tick("99"))  # market already strictly below the buy limit
    with pytest.raises(OrderRejected):
        await broker2.place_order(_order(limit="100"))


async def test_post_only_resting_fill_is_strict_and_at_the_limit() -> None:
    broker = _broker()
    await broker.place_order(_order(limit="100"))
    broker.on_tick(_tick("100"))  # a TOUCH at the level — queue unknowable, no fill
    assert not broker.emitted
    broker.on_tick(_tick("99.5"))  # strict trade-through
    fill = broker.emitted[0].fill
    assert fill is not None
    assert fill.price == Decimal("100")  # AT the limit — never the (better) tick
    assert fill.fees == Decimal("100") * fill.quantity * Decimal("0.0002")


async def test_plain_limits_keep_touch_fill_semantics() -> None:
    broker = _broker()
    await broker.place_order(_order(limit="99", post_only=False))  # rests below market
    broker.on_tick(_tick("99"))  # touch fills a NON-post-only limit (unchanged rule)
    assert broker.emitted and broker.emitted[0].fill is not None


async def test_valid_until_expires_before_filling() -> None:
    broker = _broker()
    vu = T0 + timedelta(hours=1)
    await broker.place_order(_order(limit="100", valid_until=vu, cid="ttl1"))
    broker.on_tick(_tick("99.5", ts=vu))  # ts == valid_until: still alive -> fills
    assert broker.emitted[0].fill is not None
    broker.on_tick(_tick("100", ts=vu))  # market back AT the level -> placeable again
    await broker.place_order(_order(limit="100", valid_until=vu, cid="ttl2"))
    broker.on_tick(_tick("99.5", ts=vu + timedelta(seconds=1)))  # past TTL: dies FIRST
    states = {o.client_order_id: o.state for o in await broker.get_orders()}
    assert states["ttl2"] is OrderState.CANCELLED


def _risk() -> RiskManager:
    return RiskManager(
        RiskConfig.model_validate(
            {
                "base_capital": "1000000",
                "limits": {
                    "max_gross_exposure": "1.00",
                    "max_position_per_instrument": "0.50",
                    "max_concurrent_positions": 5,
                    "max_order_value": "0.50",
                    "max_orders_per_minute": 1000,
                    "max_daily_loss_halt": "0.50",
                    "max_loss_per_trade": "0.90",
                    "per_segment_exposure_cap": "1.00",
                },
            }
        )
    )


def _signal(side: Side, qty: str, **over: object) -> Signal:
    base: dict[str, object] = {
        "strategy_id": "t",
        "symbol": SYMBOL,
        "asset_class": AssetClass.CRYPTO,
        "side": side,
        "quantity": Decimal(qty),
        "order_type": OrderType.MARKET,
        "created_at": T0,
    }
    base.update(over)
    return Signal.model_validate(base)


def _oms() -> tuple[OMS, PaperBroker, RiskManager]:
    broker = _broker()
    risk = _risk()
    store = StateStore("sqlite:///:memory:")
    store.create_schema()
    oms = OMS(adapter=broker, risk=risk, store=store, venue=Venue.BINANCE, clock=FakeClock(T0))
    return oms, broker, risk


async def test_post_only_miss_is_not_an_error_strike() -> None:
    oms, broker, risk = _oms()
    broker.on_tick(_tick("100", bid="99", ask="100"))
    for i in range(4):  # 4 consecutive GTX misses >= the 3-strike kill threshold
        sig = _signal(
            Side.BUY,
            "1",
            order_type=OrderType.LIMIT,
            limit_price=Decimal("100"),
            post_only=True,
            created_at=T0 + timedelta(minutes=i),
        )
        order = await oms.submit_signal(sig, reference_price=Decimal("100"))
        assert order is not None and order.state is OrderState.REJECTED
    assert risk.is_halted is False  # a missed maker entry is business, never an error


async def test_reduce_only_sizes_to_the_book_and_noops_flat() -> None:
    oms, _broker, risk = _oms()
    # Flat book: a reduce-only SELL yields NO order (and no error/audit spam).
    none = await oms.submit_signal(
        _signal(Side.SELL, "20", reduce_only=True), reference_price=Decimal("100")
    )
    assert none is None and risk.is_halted is False
    # Open +5 long; a reduce-only SELL 20 clips to 5 -> flat, never short.
    await oms.submit_signal(_signal(Side.BUY, "5"), reference_price=Decimal("100"))
    await oms.drain_events()
    out = await oms.submit_signal(
        _signal(Side.SELL, "20", reduce_only=True, created_at=T0 + timedelta(minutes=1)),
        reference_price=Decimal("100"),
    )
    assert out is not None and out.quantity == Decimal("5")
    await oms.drain_events()
    assert all(p.quantity == 0 for p in oms.positions)
    # ... and a reduce-only BUY against a (now flat) book noops too.
    again = await oms.submit_signal(
        _signal(Side.BUY, "5", reduce_only=True, created_at=T0 + timedelta(minutes=2)),
        reference_price=Decimal("100"),
    )
    assert again is None


class _RestOnce(Strategy):
    """Rests one post-only buy 1% below the first close, then goes quiet."""

    def __init__(self) -> None:
        self._done = False

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        if self._done:
            return []
        self._done = True
        limit = (bar.close * Decimal("0.99")).quantize(Decimal("0.01"))
        return [
            Signal(
                strategy_id="rest-once",
                symbol=bar.symbol,
                asset_class=bar.asset_class,
                side=Side.BUY,
                quantity=Decimal("1"),
                order_type=OrderType.LIMIT,
                limit_price=limit,
                post_only=True,
                created_at=bar.start + bar.interval,
            )
        ]

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []


async def test_ohlc_ticks_fill_on_the_bar_range_not_the_close() -> None:
    from alpha_core.backtest.runner import BacktestResult, run_backtest

    def bar(i: int, o: str, h: str, lo: str, c: str) -> Bar:
        return Bar(
            symbol=SYMBOL,
            venue=Venue.BINANCE,
            asset_class=AssetClass.CRYPTO,
            start=T0 + i * timedelta(hours=1),
            interval=timedelta(hours=1),
            open=Decimal(o),
            high=Decimal(h),
            low=Decimal(lo),
            close=Decimal(c),
            volume=Decimal("1"),
        )

    # Bar 0 closes at 100 -> the strategy rests a buy at 99. Bar 1's LOW pokes to
    # 98.5 but its CLOSE stays above the limit: only the OHLC tape can fill it.
    bars = [bar(0, "100", "101", "99.5", "100"), bar(1, "100", "101", "98.5", "100.5")]

    async def run(ohlc: bool) -> BacktestResult:
        return await run_backtest(
            bars=bars,
            strategy=_RestOnce(),
            instruments={SYMBOL: InstrumentMeta(asset_class=AssetClass.CRYPTO)},
            risk_config=_risk()._cfg,
            cost_config=MAKER_COSTS,
            venue=Venue.BINANCE,
            starting_cash=Decimal("1000000"),
            ohlc_ticks=ohlc,
        )

    with_range = await run(True)
    close_only = await run(False)
    # OHLC tape: the low trades through 99 -> maker fill at 99 (+ the terminal flatten).
    assert with_range.stats.num_fills == 2
    # Close-only tape: no close ever crossed 99 -> the limit never fills.
    assert close_only.stats.num_fills == 0


async def test_ccxt_adapter_refuses_unmapped_maker_fields() -> None:
    # TEST-1 parity guard (#184): the live adapter must refuse the fields it cannot
    # map yet — silently placing a plain limit would take as a taker with no TTL.
    from types import SimpleNamespace

    from alpha_core.adapters.crypto_ccxt import CcxtAdapter
    from alpha_core.core.errors import InvalidOrder

    adapter = CcxtAdapter(exchange=SimpleNamespace(), venue=Venue.BINANCE, streaming=False)
    with pytest.raises(InvalidOrder, match="not mapped"):
        await adapter.place_order(_order(post_only=True))


def test_post_only_hour_window_needs_two_bars() -> None:
    from alpha_core.strategy.examples.seasonal_window import (
        SeasonalHourLong,
        SeasonalHourLongConfig,
    )

    with pytest.raises(ValueError, match="same-bar fill/exit race"):
        SeasonalHourLong(SeasonalHourLongConfig(hold_hours=1, entry_execution="post_only"))
    SeasonalHourLong(SeasonalHourLongConfig(hold_hours=2, entry_execution="post_only"))
