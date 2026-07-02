"""Binance public-data transforms -> core Bars (B1a.1b, M3.0).

Pure, network-free converters; the HTTP/archive fetch lives in the ingest
scripts. Two free Binance sources, no key required:

- **klines** (REST or the ``data.binance.vision`` archive) — a positional row
  ``[openTime_ms, "open", "high", "low", "close", "volume", closeTime_ms, ...]``.
  The archive CSV uses the *same* column order, so :func:`klines_to_bars` parses
  both (it reads only fields 0-5). This is the 1-second-resolution cold-store path.
- **aggTrades** (the archive: tick-level executions) — a positional row
  ``[aggId, "price", "qty", firstId, lastId, transactTime_ms, isBuyerMaker]``.
  :func:`aggtrades_to_bars` folds time-ordered trades into OHLCV bars at any
  interval, so finer-than-1s bars can be derived from ticks (M3.0 crypto leg).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar


def kline_to_bar(
    kline: Sequence[Any], *, symbol: str, interval_seconds: int, venue: Venue = Venue.BINANCE
) -> Bar:
    """One Binance kline row -> a core ``Bar`` (Decimal money, tz-UTC open). ``venue`` keys the
    series in the store: futures klines (fapi) are ``BINANCE``, spot klines (api/v3 — the same
    positional row shape) are ``BINANCE_SPOT`` — the basis track's two legs must never collide."""
    return Bar(
        symbol=symbol,
        venue=venue,
        asset_class=AssetClass.CRYPTO,
        start=datetime.fromtimestamp(int(kline[0]) / 1000, tz=UTC),
        interval=timedelta(seconds=interval_seconds),
        open=Decimal(str(kline[1])),
        high=Decimal(str(kline[2])),
        low=Decimal(str(kline[3])),
        close=Decimal(str(kline[4])),
        volume=Decimal(str(kline[5])),
    )


def klines_to_bars(
    klines: Sequence[Sequence[Any]],
    *,
    symbol: str,
    interval_seconds: int,
    venue: Venue = Venue.BINANCE,
) -> list[Bar]:
    """A page of Binance klines -> ``Bar``s. Every row is taken as a closed bar; the
    caller fetches only past windows, so there is no still-forming final candle to drop.

    Works on both REST kline pages and ``data.binance.vision`` archive CSV rows (same
    column order) — a header row (non-numeric ``open_time``) is skipped. ``venue`` keys
    the series (futures ``BINANCE`` vs spot ``BINANCE_SPOT`` — see :func:`kline_to_bar`)."""
    out: list[Bar] = []
    for k in klines:
        try:
            int(k[0])  # numeric open-time -> a data row (skip a CSV header or blank line)
        except (ValueError, TypeError, IndexError):
            continue
        out.append(kline_to_bar(k, symbol=symbol, interval_seconds=interval_seconds, venue=venue))
    return out


def aggtrades_to_bars(
    rows: Iterable[Sequence[Any]],
    *,
    symbol: str,
    interval_seconds: int,
    venue: Venue = Venue.BINANCE,
) -> list[Bar]:
    """Fold time-ordered aggTrade rows into OHLCV ``Bar``s at ``interval_seconds``.

    Archive aggTrade row: ``[aggId, price, qty, firstId, lastId, transactTime_ms,
    isBuyerMaker]`` — only price (1), qty (2), and transact-time (5) are read. Trades
    are bucketed by ``floor(transactTime / interval)``; per bucket open = the first
    trade's price, close = the last, high/low = extremes, volume = summed qty. Rows
    must be time-ordered (the archive is); a header row is skipped. Empty -> ``[]``."""
    interval = timedelta(seconds=interval_seconds)
    interval_ms = interval_seconds * 1000
    bars: list[Bar] = []
    bucket_ms: int | None = None
    o = h = low = c = v = Decimal(0)

    def _flush() -> None:
        assert bucket_ms is not None
        bars.append(
            Bar(
                symbol=symbol,
                venue=venue,
                asset_class=AssetClass.CRYPTO,
                start=datetime.fromtimestamp(bucket_ms / 1000, tz=UTC),
                interval=interval,
                open=o,
                high=h,
                low=low,
                close=c,
                volume=v,
            )
        )

    for row in rows:
        try:
            ts_ms = int(row[5])  # transact-time; non-numeric/missing -> header/blank, skip
        except (ValueError, TypeError, IndexError):
            continue
        price = Decimal(str(row[1]))
        qty = Decimal(str(row[2]))
        bucket = (ts_ms // interval_ms) * interval_ms
        if bucket_ms is None:
            bucket_ms, o, h, low, c, v = bucket, price, price, price, price, qty
        elif bucket == bucket_ms:
            h, low, c, v = max(h, price), min(low, price), price, v + qty
        elif bucket > bucket_ms:
            _flush()
            bucket_ms, o, h, low, c, v = bucket, price, price, price, price, qty
        else:  # a bucket going backwards means the input wasn't time-ordered — never guess
            raise ValueError("aggTrade rows must be time-ordered (transact-time went backwards)")
    if bucket_ms is not None:
        _flush()
    return bars
