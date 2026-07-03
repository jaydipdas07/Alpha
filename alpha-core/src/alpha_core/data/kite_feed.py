"""Kite ticker → normalized ``Tick`` feed (M4.5 — live-feed paper, R9).

The LIVE Indian market-data leg: Zerodha's ticker websocket pushes binary quote
packets on a background thread; this bridges them onto the asyncio loop as the
kernel's normalized ``Tick``s, exactly shaped like every other feed — so the same
Worker/BarBuilder/strategy stack runs on live NSE data with **no order path at
all** (M4.5 pairs it with the ``PaperBroker``; a real Kite ORDER adapter is a
later, separately-gated build).

Layering (the ``CcxtAdapter`` rule): the kernel never imports ``kiteconnect`` —
the worker factory constructs the real ``KiteTicker`` and passes it in; this
module only speaks to the ``KiteTickerLike`` protocol below, so the whole feed is
testable with a fake. Money is ``Decimal`` from the edge (``Decimal(str(x))`` —
the SDK hands floats); Kite's naive-IST exchange timestamps convert to tz-aware
UTC here, at the edge.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.interfaces import DataFeed
from alpha_core.core.models import Bar, Tick
from alpha_core.observability.logging import get_logger

_IST = ZoneInfo("Asia/Kolkata")  # Kite timestamps are naive IST — convert at the edge


class KiteTickerLike(Protocol):
    """The slice of ``kiteconnect.KiteTicker`` the feed uses (assignable callbacks +
    the connection surface). The worker passes the real thing; tests pass a fake."""

    MODE_FULL: str
    # Kite's callback convention: each receives the ws client as its first argument.
    on_ticks: Callable[[Any, list[dict[str, Any]]], None] | None
    on_connect: Callable[[Any, Any], None] | None
    on_error: Callable[[Any, int, str], None] | None
    on_noreconnect: Callable[[Any], None] | None

    def connect(self, threaded: bool = ...) -> None: ...
    def subscribe(self, instrument_tokens: list[int]) -> None: ...
    def set_mode(self, mode: str, instrument_tokens: list[int]) -> None: ...
    def close(self, code: int | None = ..., reason: str | None = ...) -> None: ...


def _dec(value: object) -> Decimal:
    """SDK number (float) -> Decimal without float artifacts (the ccxt edge idiom)."""
    return Decimal(str(value))


def _pos(value: object) -> Decimal | None:
    """A positive Decimal or None — Kite pads absent depth levels with zeros."""
    try:
        d = _dec(value)
    except (InvalidOperation, ValueError):
        return None
    return d if d > 0 else None


class KiteTickerFeed(DataFeed):
    """Live NSE ticks from the Kite ticker websocket, normalized for the kernel.

    ``stream_ticks`` wires the callbacks, connects the ticker's own thread, and
    yields ``Tick``s from an asyncio queue (``call_soon_threadsafe`` is the only
    thread boundary). The ticker owns reconnection; only ``on_noreconnect`` (its
    retries exhausted) ends the stream, as the terminal error the worker's
    feed-stale machinery expects. Ticks with no usable price are dropped."""

    def __init__(
        self,
        ticker: KiteTickerLike,
        *,
        token_by_symbol: dict[str, int],
        asset_class: AssetClass = AssetClass.EQUITY,
        venue: Venue = Venue.NSE,
    ) -> None:
        self._ticker = ticker
        self._token_by_symbol = dict(token_by_symbol)
        self._asset_class = asset_class
        self._venue = venue
        self._log = get_logger("kite_feed")

    async def stream_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        missing = [s for s in symbols if s not in self._token_by_symbol]
        if missing:
            raise KeyError(f"no Kite instrument token for {missing} (refresh the dump?)")
        tokens = [self._token_by_symbol[s] for s in symbols]
        symbol_by_token = {self._token_by_symbol[s]: s for s in symbols}
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Tick | Exception] = asyncio.Queue()

        def _put(item: Tick | Exception) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        def _on_ticks(_ws: Any, raw_ticks: list[dict[str, Any]]) -> None:
            for raw in raw_ticks:
                symbol = symbol_by_token.get(raw.get("instrument_token", -1))
                if symbol is None:
                    continue  # not ours (defensive; Kite only sends subscribed tokens)
                tick = self._normalize(symbol, raw)
                if tick is not None:
                    _put(tick)

        def _on_connect(ws: Any, _response: Any) -> None:
            # (Re)subscribe on every connect — Kite forgets subscriptions across
            # reconnects. FULL mode carries depth (top-of-book bid/ask) + timestamps.
            ws.subscribe(tokens)
            ws.set_mode(self._ticker.MODE_FULL, tokens)
            self._log.info("kite_ws_connected", tokens=len(tokens))

        def _on_error(_ws: Any, code: int, reason: str) -> None:
            # Transient: the ticker reconnects on its own; surface for the log only.
            self._log.warning("kite_ws_error", code=code, reason=reason)

        def _on_noreconnect(_ws: Any) -> None:
            _put(ConnectionError("kite ticker gave up reconnecting"))

        self._ticker.on_ticks = _on_ticks
        self._ticker.on_connect = _on_connect
        self._ticker.on_error = _on_error
        self._ticker.on_noreconnect = _on_noreconnect
        self._ticker.connect(threaded=True)
        try:
            while True:
                item = await queue.get()
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            try:
                self._ticker.close()
            except Exception as exc:  # closing a dead ws must not mask the real error
                self._log.warning("kite_ws_close_failed", error=repr(exc))

    def stream_bars(self, symbols: Sequence[str]) -> AsyncIterator[Bar]:
        raise NotImplementedError("KiteTickerFeed streams ticks; BarBuilder makes the bars")

    def _normalize(self, symbol: str, raw: dict[str, Any]) -> Tick | None:
        """One Kite tick dict -> the kernel ``Tick`` (or None if unusable)."""
        last = _pos(raw.get("last_price"))
        bid = ask = None
        depth = raw.get("depth")
        if isinstance(depth, dict):
            buys = depth.get("buy") or []
            sells = depth.get("sell") or []
            if buys and isinstance(buys[0], dict):
                bid = _pos(buys[0].get("price"))
            if sells and isinstance(sells[0], dict):
                ask = _pos(sells[0].get("price"))
        if last is None and (bid is None or ask is None):
            return None  # nothing to price a bar or a paper fill with
        ts = raw.get("exchange_timestamp") or raw.get("last_trade_time")
        if isinstance(ts, datetime):
            stamped = ts.replace(tzinfo=_IST).astimezone(UTC) if ts.tzinfo is None else ts
        else:
            # The edge owns wall-clock; a missing venue stamp degrades to arrival time.
            stamped = datetime.now(UTC)
        volume = raw.get("volume_traded", raw.get("volume"))
        return Tick(
            symbol=symbol,
            venue=self._venue,
            asset_class=self._asset_class,
            ts=stamped,
            last_price=last,
            bid=bid,
            ask=ask,
            volume=_dec(volume) if volume is not None else None,
        )
