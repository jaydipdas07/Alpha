"""Tick → bar aggregation for the live loop (M3.1).

``AdapterFeed`` streams a venue's live ticks; the worker folds them into fixed
interval OHLCV bars here, then feeds closed bars to the strategy — the same
``Strategy.on_bar`` the backtester drives, so backtest ≡ live (ADR 0001). Bar
boundaries are aligned to UTC-epoch multiples of the interval (deterministic,
venue-independent), so a bar built live is the same bar the cold store would hold.

A bar is emitted (returned closed) when a tick arrives in a *later* bucket; the
still-forming current bar is held until then. ``flush`` force-closes it (shutdown).
Money is ``Decimal``; time is tz-aware UTC. No magic numbers — the interval is the
caller's (from ``config/<env>.yaml`` ``bar_interval_seconds``).

**Contract — tick-driven, one bar per *active* interval.** An interval that
receives no tick produces no bar (the builder never invents a flat bar from a
stale price — after a feed outage that would feed the strategy fiction). Cold-store
klines are continuous, so for a bar-*count* strategy to stay backtest≡live (TEST-1)
on a gappy instrument, the worker loop supplies continuous-bar semantics via a
per-interval timer flush — not the builder. For the liquid seconds-to-minutes
crypto cadence this is moot (no empty intervals). Bucket alignment assumes a
sub-daily interval (``floor(epoch / interval)``), which is the live loop's range.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar, Tick
from alpha_core.observability.logging import get_logger


@dataclass(slots=True)
class _Bucket:
    """The forming bar for one symbol."""

    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    venue: Venue
    asset_class: AssetClass


class BarBuilder:
    """Folds a per-symbol tick stream into closed interval bars (one builder, many symbols)."""

    def __init__(self, interval_seconds: int) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._interval_s = interval_seconds
        self._interval = timedelta(seconds=interval_seconds)
        self._buckets: dict[str, _Bucket] = {}
        self._log = get_logger("bar_builder")
        self.dropped_out_of_order = 0

    def _bucket_start(self, ts: datetime) -> datetime:
        epoch = int(ts.astimezone(UTC).timestamp())
        floored = (epoch // self._interval_s) * self._interval_s
        return datetime.fromtimestamp(floored, tz=UTC)

    @staticmethod
    def _price(tick: Tick) -> Decimal:
        if tick.last_price is not None:
            return tick.last_price
        # The Tick model guarantees last_price or both bid+ask; mid-price the quote.
        # A `raise` (not `assert`, stripped under -O) keeps the guarantee on the money path.
        if tick.bid is None or tick.ask is None:  # pragma: no cover - Tick model guarantees one
            raise ValueError("tick has neither last_price nor a bid/ask pair")
        return (tick.bid + tick.ask) / 2

    @staticmethod
    def _volume(tick: Tick) -> Decimal:
        if tick.volume is not None:
            return tick.volume
        return tick.last_qty if tick.last_qty is not None else Decimal(0)

    def add(self, tick: Tick) -> Bar | None:
        """Fold ``tick`` in; return the just-*closed* prior bar if this tick rolled
        into a new interval, else ``None``. An out-of-order tick (older than the
        current bucket) is dropped — a closed bar is immutable, so we never guess
        it backwards."""
        bucket = self._bucket_start(tick.ts)
        price = self._price(tick)
        volume = self._volume(tick)
        cur = self._buckets.get(tick.symbol)

        if cur is None:
            self._buckets[tick.symbol] = self._open(bucket, price, volume, tick)
            return None
        if bucket == cur.start:
            cur.high = max(cur.high, price)
            cur.low = min(cur.low, price)
            cur.close = price
            cur.volume += volume
            return None
        if bucket > cur.start:
            closed = self._to_bar(tick.symbol, cur)
            self._buckets[tick.symbol] = self._open(bucket, price, volume, tick)
            return closed
        # bucket < cur.start: a late/out-of-order tick — drop it (the prior bar may
        # already be emitted; corrupting the current one would lie). Live feeds are
        # ordered, so this should be rare; count it for observability.
        self.dropped_out_of_order += 1
        self._log.warning("tick_out_of_order", symbol=tick.symbol, ts=tick.ts.isoformat())
        return None

    def flush(self, symbol: str) -> Bar | None:
        """Force-close and return ``symbol``'s forming bar (e.g. on shutdown)."""
        cur = self._buckets.pop(symbol, None)
        return self._to_bar(symbol, cur) if cur is not None else None

    def _open(self, start: datetime, price: Decimal, volume: Decimal, tick: Tick) -> _Bucket:
        return _Bucket(start, price, price, price, price, volume, tick.venue, tick.asset_class)

    def _to_bar(self, symbol: str, b: _Bucket) -> Bar:
        return Bar(
            symbol=symbol,
            venue=b.venue,
            asset_class=b.asset_class,
            start=b.start,
            interval=self._interval,
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=b.volume,
        )
