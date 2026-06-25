"""Binance USDⓈ-M perpetual klines -> core Bars (B1a.1b).

Pure transform of Binance's public futures kline rows; the network fetch is in
``scripts/ingest_cold_store.py``. A fapi kline is a positional list:
``[openTime_ms, "open", "high", "low", "close", "volume", closeTime_ms, ...]``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar


def kline_to_bar(kline: Sequence[Any], *, symbol: str, interval_seconds: int) -> Bar:
    """One Binance fapi kline row -> a core ``Bar`` (Decimal money, tz-UTC open)."""
    return Bar(
        symbol=symbol,
        venue=Venue.BINANCE,
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
    klines: Sequence[Sequence[Any]], *, symbol: str, interval_seconds: int
) -> list[Bar]:
    """A page of Binance klines -> ``Bar``s. Every row is taken as a closed bar; the
    caller fetches only past windows, so there is no still-forming final candle to drop."""
    return [kline_to_bar(k, symbol=symbol, interval_seconds=interval_seconds) for k in klines]
