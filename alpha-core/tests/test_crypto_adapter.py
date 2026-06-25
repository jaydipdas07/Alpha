"""CcxtAdapter tests (Phase 10 B10.1) — mocked async exchange, no live calls.

The fake exchange records calls and returns CCXT-shaped dicts; SDK errors are
simulated with classes named like ``ccxt`` exceptions so the adapter's
name-based normalization is exercised without importing the optional package.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from alpha_core.adapters.crypto_ccxt import CcxtAdapter
from alpha_core.core.enums import AssetClass, OrderState, OrderType, Side, Venue
from alpha_core.core.errors import (
    AuthError,
    BrokerRateLimited,
    BrokerTimeout,
    BrokerUnavailable,
    InsufficientFunds,
    InvalidOrder,
    TerminalBrokerError,
    UnknownOrder,
)
from alpha_core.core.interfaces import BrokerEventKind, BrokerOrderEvent
from alpha_core.core.models import Order

# 2026-06-15 09:20:00 UTC in epoch ms.
TS_MS = 1_781_854_800_000


# --- SDK exception lookalikes (matched by class name) --------------------------


class AuthenticationError(Exception): ...


class OrderNotFound(Exception): ...


class NetworkError(Exception): ...


class RequestTimeout(Exception): ...


class RateLimitExceeded(Exception): ...


class _WeirdError(Exception): ...


# Give the lookalikes the exact ccxt class names the adapter matches on.
InsufficientFundsExc = type("InsufficientFunds", (Exception,), {})
InvalidOrderExc = type("InvalidOrder", (Exception,), {})


# --- fake async exchange -------------------------------------------------------


class FakeExchange:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.cancelled: list[tuple[str, str]] = []
        self.edited: list[dict[str, Any]] = []
        self._open: list[dict[str, Any]] = []
        self._positions: list[dict[str, Any]] = []
        self.create_error: Exception | None = None
        self._ids = iter(f"BIN{n}" for n in range(1, 99))
        self.ticker_batches: list[Any] = []
        self.order_batches: list[Any] = []
        self.my_trade_batches: list[Any] = []  # exact fills (watch_my_trades, R16)
        self.options: dict[str, str] = {"defaultType": "swap"}  # derivatives → positions apply

    async def create_order(
        self, symbol: str, type: str, side: str, amount: float, price: float | None, params: Any
    ) -> dict[str, Any]:
        if self.create_error is not None:
            raise self.create_error
        self.created.append(
            {
                "symbol": symbol,
                "type": type,
                "side": side,
                "amount": amount,
                "price": price,
                "params": params,
            }
        )
        return {"id": next(self._ids)}

    async def cancel_order(self, id: str, symbol: str) -> dict[str, Any]:
        self.cancelled.append((id, symbol))
        return {}

    async def edit_order(
        self, id: str, symbol: str, type: str, side: str, amount: float, price: float | None
    ) -> dict[str, Any]:
        self.edited.append({"id": id, "amount": amount, "price": price})
        return {}

    async def fetch_open_orders(self) -> list[dict[str, Any]]:
        return self._open

    async def fetch_positions(self) -> list[dict[str, Any]]:
        return self._positions

    # REST-poll surface (used by the polling path for non-websocket venues, P18.1)
    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return {}

    async def fetch_my_trades(self) -> list[dict[str, Any]]:
        return []

    async def fetch_closed_orders(self) -> list[dict[str, Any]]:
        return []

    # streaming: each call pops the next scripted trade-list for that symbol; an
    # Exception is raised; when exhausted it blocks (the consumer cancels us).
    async def watch_trades(self, symbol: str) -> list[dict[str, Any]]:
        if not self.ticker_batches:
            await asyncio.Event().wait()  # no more scripted updates -> park
        item = self.ticker_batches.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[no-any-return]

    async def watch_orders(self) -> list[dict[str, Any]]:
        if not self.order_batches:
            await asyncio.Event().wait()  # no more scripted updates -> park
        item = self.order_batches.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[no-any-return]

    async def watch_my_trades(self) -> list[dict[str, Any]]:
        if not self.my_trade_batches:
            await asyncio.Event().wait()  # no more scripted fills -> park
        item = self.my_trade_batches.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[no-any-return]


def _order(
    *,
    cid: str = "alpha-btc1",
    order_type: OrderType = OrderType.MARKET,
    limit: str | None = None,
    stop: str | None = None,
    side: Side = Side.BUY,
    qty: str = "0.5",
) -> Order:
    from datetime import UTC, datetime

    t0 = datetime(2026, 6, 15, 4, 0, tzinfo=UTC)
    return Order(
        client_order_id=cid,
        symbol="BTC/USDT",
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        side=side,
        order_type=order_type,
        quantity=Decimal(qty),
        limit_price=Decimal(limit) if limit else None,
        stop_price=Decimal(stop) if stop else None,
        state=OrderState.NEW,
        strategy_id="s1",
        created_at=t0,
        updated_at=t0,
    )


# --- placement -----------------------------------------------------------------


async def test_place_market_order_maps_fields() -> None:
    ex = FakeExchange()
    adapter = CcxtAdapter(exchange=ex)
    vid = await adapter.place_order(_order())
    assert vid == "BIN1"
    sent = ex.created[0]
    assert sent["symbol"] == "BTC/USDT"
    assert sent["type"] == "market"
    assert sent["side"] == "buy"
    assert sent["amount"] == 0.5
    assert sent["price"] is None
    assert sent["params"]["clientOrderId"] == "alpha-btc1"  # native dedup key


async def test_place_is_idempotent() -> None:
    ex = FakeExchange()
    adapter = CcxtAdapter(exchange=ex)
    order = _order()
    first = await adapter.place_order(order)
    second = await adapter.place_order(order)
    assert first == second
    assert len(ex.created) == 1


async def test_limit_and_stop_cross_the_boundary() -> None:
    ex = FakeExchange()
    adapter = CcxtAdapter(exchange=ex)
    await adapter.place_order(_order(cid="alpha-l", order_type=OrderType.LIMIT, limit="65000.5"))
    await adapter.place_order(
        _order(cid="alpha-s", order_type=OrderType.STOP_LIMIT, limit="64000", stop="64500")
    )
    assert ex.created[0]["type"] == "limit"
    assert ex.created[0]["price"] == 65000.5
    assert ex.created[1]["params"]["triggerPrice"] == 64500.0


# --- error normalization -------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (AuthenticationError("bad key"), AuthError),
        (OrderNotFound("gone"), UnknownOrder),
        (InsufficientFundsExc("no margin"), InsufficientFunds),
        (InvalidOrderExc("bad"), InvalidOrder),
        (NetworkError("down"), BrokerUnavailable),
        (RequestTimeout("slow"), BrokerTimeout),
        (RateLimitExceeded("429"), BrokerRateLimited),
        (_WeirdError("???"), TerminalBrokerError),
    ],
)
async def test_sdk_errors_are_normalized(exc: Exception, expected: type[Exception]) -> None:
    ex = FakeExchange()
    ex.create_error = exc
    adapter = CcxtAdapter(exchange=ex)
    with pytest.raises(expected):
        await adapter.place_order(_order())


# --- cancel / modify -----------------------------------------------------------


async def test_cancel_unknown_is_noop() -> None:
    ex = FakeExchange()
    adapter = CcxtAdapter(exchange=ex)
    await adapter.cancel("alpha-never")
    assert ex.cancelled == []


async def test_cancel_known_calls_exchange() -> None:
    ex = FakeExchange()
    adapter = CcxtAdapter(exchange=ex)
    vid = await adapter.place_order(_order())
    await adapter.cancel("alpha-btc1")
    assert ex.cancelled[0] == (vid, "BTC/USDT")


async def test_modify_requires_a_field() -> None:
    adapter = CcxtAdapter(exchange=FakeExchange())
    with pytest.raises(InvalidOrder):
        await adapter.modify("alpha-btc1")


async def test_modify_unknown_rejected() -> None:
    adapter = CcxtAdapter(exchange=FakeExchange())
    with pytest.raises(InvalidOrder):
        await adapter.modify("alpha-ghost", limit_price=Decimal("100"))


# --- dedup-by-query ------------------------------------------------------------


async def test_find_order_id_scans_by_client_id() -> None:
    ex = FakeExchange()
    ex._open = [
        {"id": "BIN55", "clientOrderId": "alpha-btc1"},
        {"id": "BIN99", "clientOrderId": "other"},
    ]
    adapter = CcxtAdapter(exchange=ex)
    assert await adapter.find_order_id("alpha-btc1") == "BIN55"
    assert await adapter.find_order_id("alpha-missing") is None


async def test_find_order_id_uses_cache_after_place() -> None:
    ex = FakeExchange()
    adapter = CcxtAdapter(exchange=ex)
    vid = await adapter.place_order(_order())
    assert await adapter.find_order_id("alpha-btc1") == vid


# --- broker truth mapping ------------------------------------------------------


def _ccxt_order(**over: Any) -> dict[str, Any]:
    base = {
        "id": "BIN1",
        "clientOrderId": "alpha-btc1",
        "symbol": "BTC/USDT",
        "type": "market",
        "side": "buy",
        "status": "closed",
        "amount": 0.5,
        "filled": 0.5,
        "average": 65010.0,
        "price": 0,
        "timestamp": TS_MS,
    }
    base.update(over)
    return base


async def test_get_orders_maps_closed_fill() -> None:
    ex = FakeExchange()
    ex._open = [_ccxt_order()]
    adapter = CcxtAdapter(exchange=ex)
    o = (await adapter.get_orders())[0]
    assert o.client_order_id == "alpha-btc1"
    assert o.venue_order_id == "BIN1"
    assert o.symbol == "BTC/USDT"
    assert o.asset_class is AssetClass.CRYPTO
    assert o.state is OrderState.FILLED
    assert o.average_fill_price == Decimal("65010.0")


async def test_get_orders_maps_partial() -> None:
    ex = FakeExchange()
    ex._open = [
        _ccxt_order(type="limit", status="open", filled=0.2, average=64000.0, price=64000.0)
    ]
    adapter = CcxtAdapter(exchange=ex)
    o = (await adapter.get_orders())[0]
    assert o.state is OrderState.PARTIALLY_FILLED
    assert o.limit_price == Decimal("64000.0")


async def test_get_positions_signs_and_filters() -> None:
    ex = FakeExchange()
    ex._positions = [
        {
            "symbol": "BTC/USDT",
            "contracts": 0.5,
            "side": "long",
            "entryPrice": 65000.0,
            "markPrice": 65500.0,
            "unrealizedPnl": 250.0,
            "realizedPnl": 0,
        },
        {
            "symbol": "ETH/USDT",
            "contracts": 2.0,
            "side": "short",
            "entryPrice": 3500.0,
            "markPrice": 3450.0,
            "unrealizedPnl": 100.0,
            "realizedPnl": 0,
        },
        {"symbol": "SOL/USDT", "contracts": 0, "side": "long", "entryPrice": 0},  # flat
    ]
    adapter = CcxtAdapter(exchange=ex)
    positions = await adapter.get_positions()
    by_symbol = {p.symbol: p for p in positions}
    assert set(by_symbol) == {"BTC/USDT", "ETH/USDT"}
    assert by_symbol["BTC/USDT"].quantity == Decimal("0.5")
    assert by_symbol["ETH/USDT"].quantity == Decimal("-2.0")  # short -> negative
    assert by_symbol["BTC/USDT"].unrealized_pnl == Decimal("250.0")


# --- streaming (G3) ------------------------------------------------------------


async def test_subscribe_ticks_normalizes_and_reconnects() -> None:
    ex = FakeExchange()
    # first watch drops (transient) -> reconnect; then a real trade print.
    ex.ticker_batches = [
        NetworkError("ws drop"),
        [{"price": 65000.0, "amount": 0.1, "timestamp": TS_MS}],
    ]
    adapter = CcxtAdapter(exchange=ex)
    ticks = []
    async for tick in adapter.subscribe_ticks(["BTC/USDT"]):
        ticks.append(tick)
        break  # one tick is enough — it proves reconnect + normalization
    assert ticks[0].symbol == "BTC/USDT"
    assert ticks[0].last_price == Decimal("65000.0")
    assert ticks[0].volume == Decimal("0.1")  # DATA-1: a trade print's amount is its volume


async def test_subscribe_ticks_skips_priceless_update() -> None:
    ex = FakeExchange()
    ex.ticker_batches = [
        [{"amount": 0.1, "timestamp": TS_MS}],  # no price -> skipped
        [{"price": 65000.0, "timestamp": TS_MS}],
    ]
    adapter = CcxtAdapter(exchange=ex)
    async for tick in adapter.subscribe_ticks(["BTC/USDT"]):
        assert tick.last_price == Decimal("65000.0")  # the priceless one was dropped
        break


async def test_subscribe_ticks_terminal_error_raises() -> None:
    ex = FakeExchange()
    ex.ticker_batches = [AuthenticationError("bad key")]
    adapter = CcxtAdapter(exchange=ex)
    with pytest.raises(AuthError):
        async for _ in adapter.subscribe_ticks(["BTC/USDT"]):
            pass


async def test_order_events_emits_exact_fill_and_cancel() -> None:
    # R16: the FILL comes from watch_my_trades (exact price/qty/fee + venue trade
    # id); the CANCEL comes from watch_orders. The two streams are merged.
    ex = FakeExchange()
    ex.my_trade_batches = [
        [
            {
                "id": "T-1",  # venue trade id -> fill_id
                "order": "BIN1",
                "symbol": "BTC/USDT",
                "side": "buy",
                "amount": 0.5,
                "price": 65010.0,
                "fee": {"cost": 0.65},
                "timestamp": TS_MS,
            }
        ]
    ]
    ex.order_batches = [
        [
            {
                "id": "BIN2",
                "clientOrderId": "alpha-btc2",
                "symbol": "BTC/USDT",
                "side": "sell",
                "status": "canceled",
                "timestamp": TS_MS,
            },
            {"id": "BIN3", "status": "open", "symbol": "BTC/USDT", "side": "buy"},
        ]
    ]
    adapter = CcxtAdapter(exchange=ex)
    adapter._vid_to_cid["BIN1"] = "alpha-btc1"  # as if we placed BIN1
    by_kind: dict[BrokerEventKind, BrokerOrderEvent] = {}
    async for ev in adapter.order_events():
        by_kind[ev.kind] = ev
        if len(by_kind) == 2:  # one FILL + one CANCEL (the 'open' snapshot yields nothing)
            break
    fill = by_kind[BrokerEventKind.FILL].fill
    assert fill is not None
    assert fill.fill_id == "T-1" and fill.client_order_id == "alpha-btc1"
    assert fill.price == Decimal("65010.0") and fill.quantity == Decimal("0.5")
    assert fill.fees == Decimal("0.65")
    assert BrokerEventKind.CANCEL in by_kind


# --- resilience: retry on transient errors (G8) --------------------------------


async def test_idempotent_read_retries_on_transient() -> None:
    from alpha_core.helpers.config import RetryConfig

    class FlakyExchange(FakeExchange):
        def __init__(self) -> None:
            super().__init__()
            self._attempts = 0

        async def fetch_open_orders(self) -> list[dict[str, Any]]:
            self._attempts += 1
            if self._attempts == 1:
                raise NetworkError("transient blip")  # -> BrokerUnavailable
            return []

    ex = FlakyExchange()
    retry = RetryConfig(max_attempts=3, base_backoff=0.0, max_backoff=0.0, jitter=False)
    adapter = CcxtAdapter(exchange=ex, retry=retry)
    assert await adapter.get_orders() == []  # retried past the transient error
    assert ex._attempts == 2


async def test_placement_does_not_retry() -> None:
    from alpha_core.helpers.config import RetryConfig

    ex = FakeExchange()
    ex.create_error = NetworkError("blip")  # transient, but placement must not retry
    retry = RetryConfig(max_attempts=5, base_backoff=0.0, max_backoff=0.0, jitter=False)
    adapter = CcxtAdapter(exchange=ex, retry=retry)
    with pytest.raises(BrokerUnavailable):
        await adapter.place_order(_order())


async def test_partial_fills_emit_per_trade() -> None:
    # R16: a partially-filled order produces one trade per execution -> one exact
    # FILL per trade (0.4 then 0.6), each with its own venue trade id.
    ex = FakeExchange()
    ex.my_trade_batches = [
        [
            {
                "id": "T-1",
                "order": "BIN1",
                "symbol": "BTC/USDT",
                "side": "buy",
                "amount": 0.4,
                "price": 65000.0,
                "fee": {"cost": 0.26},
                "timestamp": TS_MS,
            }
        ],
        [
            {
                "id": "T-2",
                "order": "BIN1",
                "symbol": "BTC/USDT",
                "side": "buy",
                "amount": 0.6,
                "price": 65005.0,
                "fee": {"cost": 0.39},
                "timestamp": TS_MS,
            }
        ],
    ]
    adapter = CcxtAdapter(exchange=ex)
    adapter._vid_to_cid["BIN1"] = "alpha-x"
    events = []
    async for ev in adapter.order_events():
        events.append(ev)
        if len(events) == 2:
            break
    assert events[0].fill is not None and events[0].fill.quantity == Decimal("0.4")
    assert events[1].fill is not None and events[1].fill.quantity == Decimal("0.6")
    assert {e.fill.fill_id for e in events if e.fill} == {"T-1", "T-2"}


class RestOnlyExchange:
    """No ``watch_*`` methods -> CcxtAdapter detects no websockets and POLLS the
    REST surface instead (the Delta path, P18.1)."""

    def __init__(self) -> None:
        self.ticker: dict[str, Any] = {"last": 65000.0, "timestamp": TS_MS}
        self.ticker_error: Exception | None = None
        self.my_trades_script: list[list[dict[str, Any]]] = []
        self.closed_script: list[list[dict[str, Any]]] = []

    async def create_order(self, *a: Any, **k: Any) -> dict[str, Any]:
        raise NotImplementedError

    async def cancel_order(self, *a: Any, **k: Any) -> dict[str, Any]:
        raise NotImplementedError

    async def edit_order(self, *a: Any, **k: Any) -> dict[str, Any]:
        raise NotImplementedError

    async def fetch_open_orders(self) -> list[dict[str, Any]]:
        return []

    async def fetch_positions(self) -> list[dict[str, Any]]:
        return []

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        if self.ticker_error is not None:
            err, self.ticker_error = self.ticker_error, None  # raise once, then recover
            raise err
        return self.ticker

    async def fetch_my_trades(self) -> list[dict[str, Any]]:
        return self.my_trades_script.pop(0) if self.my_trades_script else []

    async def fetch_closed_orders(self) -> list[dict[str, Any]]:
        return self.closed_script.pop(0) if self.closed_script else []


def _poll_trade(tid: str) -> dict[str, Any]:
    return {
        "id": tid,
        "clientOrderId": "alpha-poll1",
        "order": "V9",
        "symbol": "BTC/USD:USD",
        "side": "buy",
        "amount": 1.0,
        "price": 64000.0,
        "fee": {"cost": 0.5, "currency": "USD"},
        "timestamp": TS_MS,
    }


async def test_rest_only_polls_fill_from_my_trades() -> None:
    # A new my-trade on a later poll -> a FILL event (P18.1 polling fill path).
    ex = RestOnlyExchange()
    ex.my_trades_script = [[], [_poll_trade("t1")]]  # poll 1 seeds (empty), poll 2 emits
    adapter = CcxtAdapter(exchange=ex, streaming=False, poll_interval=0.001)  # type: ignore[arg-type]
    async for ev in adapter.order_events():
        assert ev.kind is BrokerEventKind.FILL
        assert ev.client_order_id == "alpha-poll1"
        assert ev.fill is not None and ev.fill.price == Decimal("64000.0")
        break


async def test_rest_only_seeded_history_not_replayed() -> None:
    # A trade present on the FIRST poll is seeded, not emitted as fresh.
    ex = RestOnlyExchange()
    ex.my_trades_script = [[_poll_trade("old")]]  # only the seed poll has a trade
    adapter = CcxtAdapter(exchange=ex, streaming=False, poll_interval=0.001)  # type: ignore[arg-type]
    import asyncio

    agen = adapter.order_events()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(agen.__anext__(), timeout=0.05)  # nothing emitted
    await agen.aclose()  # type: ignore[attr-defined]  # it's an async generator


async def test_rest_only_tick_poll_reconnects_then_surfaces_terminal() -> None:
    # A transient ticker error is retried; an auth error surfaces (P18.1).
    ex = RestOnlyExchange()
    ex.ticker_error = NetworkError("blip")  # transient -> retried, then a tick
    adapter = CcxtAdapter(exchange=ex, streaming=False, poll_interval=0.001)  # type: ignore[arg-type]
    async for tick in adapter.subscribe_ticks(["BTC/USD:USD"]):
        assert tick.last_price == Decimal("65000.0")
        break
    ex2 = RestOnlyExchange()
    ex2.ticker_error = AuthenticationError("bad key")  # terminal -> surfaces
    adapter2 = CcxtAdapter(exchange=ex2, streaming=False, poll_interval=0.001)  # type: ignore[arg-type]
    with pytest.raises(AuthError):
        async for _ in adapter2.subscribe_ticks(["BTC/USD:USD"]):
            pass


async def test_rest_only_venue_polls_ticks() -> None:
    # No watch_* -> the adapter polls fetch_ticker for ticks (P18.1, Delta).
    ex = RestOnlyExchange()
    adapter = CcxtAdapter(exchange=ex, streaming=False, poll_interval=0.001)  # type: ignore[arg-type]
    assert adapter._streaming is False
    async for tick in adapter.subscribe_ticks(["BTC/USD:USD"]):
        assert tick.symbol == "BTC/USD:USD"
        assert tick.last_price == Decimal("65000.0")
        break


async def test_rest_only_order_events_seed_then_emit() -> None:
    # First poll seeds (no replay of history); a NEW terminal order on a later
    # poll is emitted (P18.1).
    ex = RestOnlyExchange()
    ex.closed_script = [[], [{"id": "V1", "clientOrderId": "alpha-x", "status": "canceled"}]]
    adapter = CcxtAdapter(exchange=ex, streaming=False, poll_interval=0.001)  # type: ignore[arg-type]
    async for ev in adapter.order_events():
        assert ev.client_order_id == "alpha-x"
        assert ev.kind is BrokerEventKind.CANCEL
        break


async def test_order_events_ws_stream_reconnects_then_emits() -> None:
    # The ccxt.pro watch path: a transient drop reconnects, then a terminal event
    # is emitted (covers the _stream reconnect branch).
    ex = FakeExchange()
    ex.order_batches = [
        NetworkError("ws drop"),
        [{"id": "V1", "clientOrderId": "alpha-x", "status": "canceled"}],
    ]
    adapter = CcxtAdapter(exchange=ex)  # streaming default
    async for ev in adapter.order_events():
        assert ev.kind is BrokerEventKind.CANCEL and ev.client_order_id == "alpha-x"
        break


async def test_order_events_ws_stream_surfaces_terminal_error() -> None:
    ex = FakeExchange()
    ex.order_batches = [AuthenticationError("bad key")]  # terminal -> surfaces
    adapter = CcxtAdapter(exchange=ex)
    with pytest.raises(AuthError):
        async for _ in adapter.order_events():
            pass
