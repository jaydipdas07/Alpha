"""Kite Connect historical candles -> core Bars (M3.0, Indian equities, 1-minute+).

Pure transform of the ``KiteConnect.historical_data()`` output (a list of OHLCV rows); the network
fetch + the daily-token auth (the ``KITE_ACCESS_TOKEN_AT``-staleness → 2FA reauth flow) live in the
ingest script, never here — same split as ``binance.py`` / ``yahoo.py`` (no broker SDK in the
kernel). Kite timestamps are **IST** (``Asia/Kolkata``); they are converted to tz-aware **UTC** so
the cold store is uniform (the engine never reads a non-UTC time). The caller passes the internal
store symbol (e.g. ``NSE:RELIANCE``) and the bar interval in seconds (60 for ``minute``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar


def candles_to_bars(
    candles: Sequence[Mapping[str, Any]], *, symbol: str, interval_seconds: int
) -> list[Bar]:
    """``KiteConnect.historical_data()`` rows -> ``Bar``s for ``symbol`` (NSE equity).

    Each row is a mapping ``{date, open, high, low, close, volume}`` — ``date`` a tz-aware datetime
    (the SDK returns IST). Rows with a missing OHLCV field are skipped (defensive; Kite is normally
    clean). ``interval_seconds`` sets ``Bar.interval`` (e.g. 60 for ``minute``, 86400 for ``day``).
    """
    interval = timedelta(seconds=interval_seconds)
    bars: list[Bar] = []
    for c in candles:
        ts, o, h, low, close, v = (
            c.get("date"),
            c.get("open"),
            c.get("high"),
            c.get("low"),
            c.get("close"),
            c.get("volume"),
        )
        if ts is None or None in (o, h, low, close, v):
            continue
        if not isinstance(ts, datetime):
            raise TypeError(f"Kite candle 'date' must be a datetime, got {type(ts).__name__}")
        if ts.tzinfo is None:
            raise ValueError(f"Kite candle 'date' must be tz-aware (IST); got naive {ts!r}")
        bars.append(
            Bar(
                symbol=symbol,
                venue=Venue.NSE,
                asset_class=AssetClass.EQUITY,
                start=ts.astimezone(UTC),  # IST -> UTC; the store/engine is uniform UTC
                interval=interval,
                open=Decimal(str(o)),
                high=Decimal(str(h)),
                low=Decimal(str(low)),
                close=Decimal(str(close)),
                volume=Decimal(str(v)),  # NSE volume is whole shares (Kite gives an int)
            )
        )
    return bars
