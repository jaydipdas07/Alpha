"""CcxtAdapter — crypto venues via CCXT (build/test on Binance testnet), ADR 0004.

Satisfies the ``BrokerAdapter`` contract identically to ``PaperBroker`` and
``KiteAdapter``; the venue-agnostic core is unchanged. Covers the REST surface
(place / cancel / modify / orders / positions / dedup-by-query) and the
**ccxt.pro websocket streams** (`subscribe_ticks` / `order_events`, with
reconnect on a transient drop). Live testnet validation is B10.4; the
funding/TDS cash-flow hook is B10.3.

Boundary rules (CLAUDE.md invariants):
- **Money/quantity cross the SDK boundary as the venue wants them** (``float``)
  and convert straight back to ``Decimal`` (via ``str``) on read.
- **Time** from CCXT is epoch milliseconds (UTC) — converted to tz-aware UTC.
- **Errors** normalized to the ADR-0004 taxonomy. ``AuthenticationError`` →
  ``AuthError`` (re-auth is the human's job — halt + alert, never auto-handled).

Dedup (B10.1/H8): CCXT carries our ``clientOrderId`` natively, so placement is
idempotent — ``find_order_id`` looks the order up by client id so a lost ack is
adopted, never duplicated.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from alpha_core.core.enums import AssetClass, OrderState, OrderType, Side, Venue
from alpha_core.core.errors import (
    AuthError,
    BrokerError,
    BrokerRateLimited,
    BrokerTimeout,
    BrokerUnavailable,
    InsufficientFunds,
    InvalidOrder,
    OrderRejected,
    TerminalBrokerError,
    TransientBrokerError,
    UnknownOrder,
)
from alpha_core.core.interfaces import BrokerAdapter, BrokerEventKind, BrokerOrderEvent
from alpha_core.core.models import Fill, Order, Position, Tick
from alpha_core.helpers.config import RetryConfig
from alpha_core.helpers.ratelimit import RateLimiter
from alpha_core.helpers.retry import retry_async
from alpha_core.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

_TYPE_TO_CCXT = {
    OrderType.MARKET: "market",
    OrderType.LIMIT: "limit",
    OrderType.STOP: "market",  # trigger carried via params
    OrderType.STOP_LIMIT: "limit",
}
_SIDE_TO_CCXT = {Side.BUY: "buy", Side.SELL: "sell"}
_CCXT_TO_SIDE = {"buy": Side.BUY, "sell": Side.SELL}

# CCXT unified order status -> our state (partial fills resolved separately).
_STATUS_TO_STATE = {
    "open": OrderState.OPEN,
    "closed": OrderState.FILLED,
    "canceled": OrderState.CANCELLED,
    "cancelled": OrderState.CANCELLED,
    "expired": OrderState.EXPIRED,
    "rejected": OrderState.REJECTED,
}

# CCXT exception class name -> our taxonomy. Matched by name so the optional
# ``ccxt`` package need not be importable (it is absent in CI). MRO order means
# the most-derived match wins (e.g. OrderNotFound before InvalidOrder).
_TERMINAL_BY_NAME: dict[str, type[BrokerError]] = {
    "AuthenticationError": AuthError,
    "PermissionDenied": AuthError,
    "InsufficientFunds": InsufficientFunds,
    "OrderNotFound": UnknownOrder,
    "InvalidOrder": InvalidOrder,
    "BadSymbol": InvalidOrder,
    "BadRequest": InvalidOrder,
    "ExchangeError": OrderRejected,
}
_TRANSIENT_BY_NAME: dict[str, type[BrokerError]] = {
    "RequestTimeout": BrokerTimeout,
    "RateLimitExceeded": BrokerRateLimited,
    "DDoSProtection": BrokerRateLimited,
    "OnMaintenance": BrokerUnavailable,
    "ExchangeNotAvailable": BrokerUnavailable,
    "NetworkError": BrokerUnavailable,
}


def _translate(exc: Exception) -> BrokerError:
    """Map a CCXT / network exception to the ADR-0004 taxonomy (fail fast)."""
    for cls in type(exc).__mro__:
        if cls.__name__ in _TERMINAL_BY_NAME:
            return _TERMINAL_BY_NAME[cls.__name__](str(exc) or cls.__name__)
        if cls.__name__ in _TRANSIENT_BY_NAME:
            return _TRANSIENT_BY_NAME[cls.__name__](str(exc) or cls.__name__)
    return TerminalBrokerError(f"unmapped ccxt error: {type(exc).__name__}: {exc}")


def _dec(value: object) -> Decimal:
    """Venue number -> Decimal without float artifacts."""
    return Decimal(str(value))


def _ts(ms: object) -> datetime:
    """CCXT epoch-millisecond timestamp (UTC) -> tz-aware UTC datetime."""
    return datetime.fromtimestamp(int(float(str(ms))) / 1000, tz=UTC)


class CcxtExchange(Protocol):
    """The slice of an async ``ccxt`` exchange this adapter uses (structural)."""

    async def create_order(
        self, symbol: str, type: str, side: str, amount: float, price: float | None, params: Any
    ) -> dict[str, Any]: ...
    async def cancel_order(self, id: str, symbol: str) -> dict[str, Any]: ...
    async def edit_order(
        self, id: str, symbol: str, type: str, side: str, amount: float, price: float | None
    ) -> dict[str, Any]: ...
    async def fetch_open_orders(self) -> list[dict[str, Any]]: ...
    async def fetch_positions(self) -> list[dict[str, Any]]: ...
    async def fetch_ticker(self, symbol: str) -> dict[str, Any]: ...
    async def fetch_my_trades(self) -> list[dict[str, Any]]: ...
    async def fetch_closed_orders(self) -> list[dict[str, Any]]: ...
    # websocket surface (ccxt.pro only; REST-only venues are polled — P18.1)
    async def watch_trades(self, symbol: str) -> list[dict[str, Any]]: ...
    async def watch_orders(self) -> list[dict[str, Any]]: ...
    async def watch_my_trades(self) -> list[dict[str, Any]]: ...


class CcxtAdapter(BrokerAdapter):
    """Crypto adapter over an async CCXT exchange (REST surface; ADR 0004)."""

    def __init__(
        self,
        *,
        exchange: CcxtExchange,
        venue: Venue = Venue.BINANCE,
        retry: RetryConfig | None = None,
        rate_limiter: RateLimiter | None = None,
        streaming: bool = True,
        poll_interval: float = 1.0,
    ) -> None:
        self._ex = exchange
        self._venue = venue
        self._retry = retry  # retries idempotent reads/cancel on transient errors (G8)
        self._rate = rate_limiter  # proactive API rate cap (G8)
        self._known: dict[str, Order] = {}  # client_order_id -> last-known order
        self._vid_to_cid: dict[str, str] = {}  # venue_order_id -> client_order_id (R16 fills)
        # ccxt.pro streams via watch_* (websocket); REST-only venues (e.g. Delta)
        # poll fetch_* every poll_interval instead (P18.1). The factory passes this
        # — async_support stubs watch_*, so hasattr can't detect real streaming.
        self._streaming = streaming
        self._poll_interval = poll_interval
        self._log = get_logger("ccxt")

    async def _call[T](self, fn: Callable[[], Awaitable[T]], *, idempotent: bool) -> T:
        """Rate-limit, run, and normalize one SDK call. Idempotent calls retry on
        transient errors; placement/modify do not (the OMS dedup-by-query owns
        placement idempotency, ADR 0004 H8)."""
        if self._rate is not None:
            await self._rate.acquire()

        async def once() -> T:
            try:
                return await fn()
            except Exception as exc:
                raise _translate(exc) from exc

        if idempotent and self._retry is not None:
            return await retry_async(once, self._retry)
        return await once()

    # --- placement -------------------------------------------------------------

    async def place_order(self, order: Order) -> str:
        existing = self._known.get(order.client_order_id)
        if existing is not None and existing.venue_order_id is not None:
            return existing.venue_order_id  # idempotent: never place the same intent twice
        params: dict[str, Any] = {"clientOrderId": order.client_order_id}
        if order.stop_price is not None:
            params["triggerPrice"] = float(order.stop_price)
        price = float(order.limit_price) if order.limit_price is not None else None
        result = await self._call(
            lambda: self._ex.create_order(
                order.symbol,
                _TYPE_TO_CCXT[order.order_type],
                _SIDE_TO_CCXT[order.side],
                float(order.quantity),
                price,
                params,
            ),
            idempotent=False,
        )
        venue_order_id = str(result["id"])
        self._known[order.client_order_id] = order.model_copy(
            update={"venue_order_id": venue_order_id}
        )
        self._vid_to_cid[venue_order_id] = order.client_order_id  # for watch_my_trades (R16)
        return venue_order_id

    async def cancel(self, client_order_id: str) -> None:
        known = self._known.get(client_order_id)
        if known is None or known.venue_order_id is None:
            return  # idempotent: nothing we placed -> safe no-op
        vid, symbol = known.venue_order_id, known.symbol
        await self._call(lambda: self._ex.cancel_order(vid, symbol), idempotent=True)

    async def modify(
        self,
        client_order_id: str,
        *,
        quantity: Decimal | None = None,
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
    ) -> None:
        if quantity is None and limit_price is None and stop_price is None:
            raise InvalidOrder("modify requires at least one field")
        known = self._known.get(client_order_id)
        if known is None or known.venue_order_id is None:
            raise InvalidOrder(f"no known venue order for {client_order_id}")
        amount = float(quantity) if quantity is not None else float(known.quantity)
        price = (
            float(limit_price)
            if limit_price is not None
            else (float(known.limit_price) if known.limit_price is not None else None)
        )
        vid, symbol = known.venue_order_id, known.symbol
        otype, side = _TYPE_TO_CCXT[known.order_type], _SIDE_TO_CCXT[known.side]
        await self._call(
            lambda: self._ex.edit_order(vid, symbol, otype, side, amount, price),
            idempotent=False,
        )

    # --- broker truth ----------------------------------------------------------

    async def get_orders(self) -> list[Order]:
        raw = await self._call(self._ex.fetch_open_orders, idempotent=True)
        return [self._to_order(o) for o in raw]

    async def get_positions(self) -> list[Position]:
        # Spot accounts hold balances, not positions — and Binance's
        # fetch_positions hits the futures API (fails on a spot-only key). Only
        # derivatives (swap/future) have positions to reconcile.
        if getattr(self._ex, "options", {}).get("defaultType", "spot") == "spot":
            return []
        raw = await self._call(self._ex.fetch_positions, idempotent=True)
        return [self._to_position(p) for p in raw if _dec(p.get("contracts") or 0) != 0]

    async def aclose(self) -> None:
        """Close the ccxt session's HTTP/websocket connectors (ccxt requires an
        explicit `await exchange.close()`)."""
        close = getattr(self._ex, "close", None)
        if close is not None:
            await close()

    async def find_order_id(self, client_order_id: str) -> str | None:
        known = self._known.get(client_order_id)
        if known is not None and known.venue_order_id is not None:
            return known.venue_order_id
        raw = await self._call(self._ex.fetch_open_orders, idempotent=True)
        for o in raw:
            if o.get("clientOrderId") == client_order_id:
                return str(o["id"])
        return None

    # --- streaming (ccxt.pro; G3) ----------------------------------------------

    async def subscribe_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        """Yield normalized ticks from per-symbol ccxt.pro ``watch_trades`` streams
        — the trade print is the broadly-supported market-data feed (ticker
        websocket methods aren't implemented on every venue, e.g. Binance) and is
        exactly what the bar builder needs (last traded price). Each symbol runs
        its own reconnecting watch loop, merged here through a queue; the adapter
        owns reconnect (ADR 0004). A terminal error (auth) surfaces after translation."""
        queue: asyncio.Queue[Tick | Exception] = asyncio.Queue()
        tasks = [asyncio.create_task(self._watch_symbol(s, queue)) for s in symbols]
        try:
            while True:
                item = await queue.get()
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch_symbol(self, symbol: str, queue: asyncio.Queue[Tick | Exception]) -> None:
        """One symbol's reconnecting trade-watch loop; pushes a tick per trade (or
        a terminal error) onto the shared queue."""
        if not self._streaming:
            return await self._poll_symbol(symbol, queue)
        while True:
            try:
                trades = await self._ex.watch_trades(symbol)
            except Exception as exc:
                terminal = self._classify(exc, "ticks")
                if terminal is None:
                    # transient ws drop -> reconnect, but back off first so a venue
                    # stuck returning errors can't spin this watch loop at 100% CPU
                    # (the REST poll path already sleeps; match it here, S3).
                    await asyncio.sleep(self._poll_interval)
                    continue
                await queue.put(terminal)
                return
            for trade in trades:
                tick = self._tick_from_trade(symbol, trade)
                if tick is not None:
                    await queue.put(tick)

    async def _poll_symbol(self, symbol: str, queue: asyncio.Queue[Tick | Exception]) -> None:
        """REST-polling tick loop for venues without websockets (P18.1): one
        ``fetch_ticker`` per interval → a last-price tick. A transient error is
        logged and retried; a terminal (auth) error surfaces."""
        while True:
            try:
                ticker = await self._ex.fetch_ticker(symbol)
            except Exception as exc:
                terminal = self._classify(exc, "ticks")
                if terminal is None:
                    await asyncio.sleep(self._poll_interval)
                    continue
                await queue.put(terminal)
                return
            tick = self._tick_from_ticker(symbol, ticker)
            if tick is not None:
                await queue.put(tick)
            await asyncio.sleep(self._poll_interval)

    def _tick_from_ticker(self, symbol: str, ticker: dict[str, Any]) -> Tick | None:
        """A normalized tick from a ccxt ticker (REST poll), or None if no price."""
        price = ticker.get("last") or ticker.get("close")
        if not price:
            return None
        ts = _ts(ticker["timestamp"]) if ticker.get("timestamp") else datetime.now(UTC)
        return Tick(
            symbol=symbol,
            venue=self._venue,
            asset_class=AssetClass.CRYPTO,
            ts=ts,
            last_price=_dec(price),
            last_qty=None,
        )

    async def order_events(self) -> AsyncIterator[BrokerOrderEvent]:
        """Yield normalized order events, merging two ccxt.pro streams (R16):
        **`watch_my_trades`** for FILLs (exact per-trade price/qty/fee, each with the
        venue's own trade id) and **`watch_orders`** for the terminal CANCEL/REJECT/
        EXPIRE transitions. Splitting them gives exact fills instead of an order's
        cumulative average. Each loop owns its reconnect; a terminal error surfaces.
        Venues without websockets (Delta) poll the same ``fetch_*`` instead (P18.1)."""
        queue: asyncio.Queue[BrokerOrderEvent | Exception] = asyncio.Queue()
        if not self._streaming:
            tasks = [asyncio.create_task(self._poll_order_events(queue))]
        else:
            tasks = [
                asyncio.create_task(
                    self._stream(self._ex.watch_my_trades, self._fill_event, queue)
                ),
                asyncio.create_task(
                    self._stream(self._ex.watch_orders, self._terminal_event, queue)
                ),
            ]
        try:
            while True:
                item = await queue.get()
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _stream(
        self,
        watch: Callable[[], Awaitable[list[dict[str, Any]]]],
        to_event: Callable[[dict[str, Any]], BrokerOrderEvent | None],
        queue: asyncio.Queue[BrokerOrderEvent | Exception],
    ) -> None:
        """Drive one ccxt.pro watch loop; map each item to an event onto the queue."""
        while True:
            try:
                batch = await watch()
            except Exception as exc:
                terminal = self._classify(exc, "orders")
                if terminal is None:
                    continue  # transient ws drop -> reconnect
                await queue.put(terminal)
                return
            for raw in batch:
                event = to_event(raw)
                if event is not None:
                    await queue.put(event)

    async def _poll_order_events(self, queue: asyncio.Queue[BrokerOrderEvent | Exception]) -> None:
        """REST-polling order-event loop for non-websocket venues (P18.1): each
        interval poll my-trades (FILLs) + closed orders (terminal), emitting only
        rows unseen since the last poll. The first poll *seeds* the seen-sets
        without emitting, so startup never replays history as fresh events."""
        seen_fills: set[str] = set()
        seen_terminal: set[str] = set()
        seeded = False
        while True:
            try:
                trades = await self._ex.fetch_my_trades()
                orders = await self._ex.fetch_closed_orders()
            except Exception as exc:
                terminal = self._classify(exc, "orders")
                if terminal is None:
                    await asyncio.sleep(self._poll_interval)
                    continue
                await queue.put(terminal)
                return
            for trade in trades:
                tid = str(trade.get("id") or "")
                if not tid or tid in seen_fills:
                    continue
                seen_fills.add(tid)
                if seeded:
                    event = self._fill_event(trade)
                    if event is not None:
                        await queue.put(event)
            for o in orders:
                oid = str(o.get("id") or "")
                if not oid or oid in seen_terminal:
                    continue
                seen_terminal.add(oid)
                if seeded:
                    event = self._terminal_event(o)
                    if event is not None:
                        await queue.put(event)
            seeded = True
            await asyncio.sleep(self._poll_interval)

    def _classify(self, exc: Exception, stream: str) -> Exception | None:
        """Return the TERMINAL error to surface, or None if it's a transient ws
        drop (logged; the caller reconnects)."""
        err = _translate(exc)
        if isinstance(err, TransientBrokerError):
            self._log.warning("ccxt_ws_reconnect", stream=stream, error=str(err))
            return None
        return err

    # --- mapping ---------------------------------------------------------------

    def _to_order(self, o: dict[str, Any]) -> Order:
        order_type = OrderType.LIMIT if o["type"] == "limit" else OrderType.MARKET
        filled = _dec(o.get("filled") or 0)
        state = self._state_of(str(o["status"]), filled)
        client_order_id = str(o.get("clientOrderId") or o["id"])
        return Order(
            client_order_id=client_order_id,
            venue_order_id=str(o["id"]),
            symbol=str(o["symbol"]),
            venue=self._venue,
            asset_class=AssetClass.CRYPTO,
            side=_CCXT_TO_SIDE[str(o["side"])],
            order_type=order_type,
            quantity=_dec(o["amount"]),
            limit_price=_dec(o["price"]) if order_type is OrderType.LIMIT else None,
            state=state,
            filled_quantity=filled,
            average_fill_price=_dec(o["average"]) if filled > 0 else None,
            strategy_id=client_order_id,
            created_at=_ts(o["timestamp"]),
            updated_at=_ts(o["timestamp"]),
        )

    @staticmethod
    def _state_of(status: str, filled: Decimal) -> OrderState:
        if status not in _STATUS_TO_STATE:
            raise TerminalBrokerError(f"unknown ccxt order status: {status!r}")
        state = _STATUS_TO_STATE[status]
        if state is OrderState.OPEN and filled > 0:
            return OrderState.PARTIALLY_FILLED
        return state

    def _to_position(self, p: dict[str, Any]) -> Position:
        contracts = _dec(p.get("contracts") or 0)
        quantity = -contracts if str(p.get("side")) == "short" else contracts
        return Position(
            symbol=str(p["symbol"]),
            venue=self._venue,
            asset_class=AssetClass.CRYPTO,
            quantity=quantity,
            average_price=_dec(p["entryPrice"]) if quantity != 0 else None,
            realized_pnl=_dec(p.get("realizedPnl") or 0),
            unrealized_pnl=_dec(p["unrealizedPnl"]) if p.get("unrealizedPnl") is not None else None,
            last_price=_dec(p["markPrice"]) if p.get("markPrice") else None,
            updated_at=datetime.now(UTC),
        )

    def _tick_from_trade(self, symbol: str, trade: dict[str, Any]) -> Tick | None:
        """A normalized tick from a ccxt trade print, or None if it carries no
        usable price."""
        price = trade.get("price")
        if not price:
            return None
        ts = _ts(trade["timestamp"]) if trade.get("timestamp") else datetime.now(UTC)
        # A trade print is already incremental (one trade), so its amount is both the
        # last-traded qty and this tick's volume contribution (DATA-1; the bar sums them).
        amount = _dec(trade["amount"]) if trade.get("amount") else None
        return Tick(
            symbol=symbol,
            venue=self._venue,
            asset_class=AssetClass.CRYPTO,
            ts=ts,
            last_price=_dec(price),
            last_qty=amount,
            volume=amount,
        )

    def _fill_event(self, trade: dict[str, Any]) -> BrokerOrderEvent | None:
        """Map one ccxt **trade** (an actual execution) to a FILL event with the
        exact price/qty/fee and the venue's unique trade id (R16). Trades for
        orders we didn't place (no client-order mapping) are skipped."""
        vid = str(trade.get("order") or "")
        cid = self._vid_to_cid.get(vid) or str(trade.get("clientOrderId") or "")
        if not cid:
            return None  # not one of our orders
        fee = trade.get("fee") or {}
        fill = Fill(
            fill_id=str(trade["id"]),  # the venue's own trade id — unique per execution
            client_order_id=cid,
            venue_order_id=vid,
            symbol=str(trade["symbol"]),
            venue=self._venue,
            asset_class=AssetClass.CRYPTO,
            side=_CCXT_TO_SIDE[str(trade["side"])],
            quantity=_dec(trade["amount"]),
            price=_dec(trade["price"]),
            fees=_dec(fee["cost"]) if fee.get("cost") is not None else None,
            ts=_ts(trade["timestamp"]) if trade.get("timestamp") else datetime.now(UTC),
        )
        return BrokerOrderEvent(
            kind=BrokerEventKind.FILL, client_order_id=cid, venue_order_id=vid, fill=fill
        )

    def _terminal_event(self, o: dict[str, Any]) -> BrokerOrderEvent | None:
        """Map a ccxt order snapshot to a terminal CANCEL/REJECT/EXPIRE event.
        Fills come from `watch_my_trades` (see `_fill_event`); a `closed` status is
        full-fill, already covered by the fill stream, so it's ignored here."""
        cid = str(o.get("clientOrderId") or o["id"])
        vid = str(o["id"])
        terminal = {
            "canceled": BrokerEventKind.CANCEL,
            "cancelled": BrokerEventKind.CANCEL,
            "rejected": BrokerEventKind.REJECT,
            "expired": BrokerEventKind.EXPIRE,
        }.get(str(o["status"]))
        if terminal is not None:
            self._vid_to_cid.pop(vid, None)
            return BrokerOrderEvent(kind=terminal, client_order_id=cid, venue_order_id=vid)
        return None
