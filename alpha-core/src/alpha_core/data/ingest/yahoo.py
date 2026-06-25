"""Yahoo Finance v8 chart JSON -> core Bars (B1a.1b, equity daily).

Pure transform of the chart payload; the network fetch is in the ingest script.
NSE constituents are the ``<TICKER>.NS`` Yahoo symbols (e.g. ``RELIANCE.NS``);
the caller passes the internal symbol to store under (e.g. ``NSE:RELIANCE``).
Yahoo leaves holidays/halts as ``null`` in the quote arrays — those rows are skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar

_ONE_DAY = timedelta(days=1)


def chart_to_bars(payload: dict[str, Any], *, symbol: str) -> list[Bar]:
    """A Yahoo v8 chart payload -> daily ``Bar``s for ``symbol`` (NSE equity)."""
    result = payload["chart"]["result"][0]
    stamps: list[int] = result["timestamp"]
    quote: dict[str, list[Any]] = result["indicators"]["quote"][0]

    bars: list[Bar] = []
    for i, ts in enumerate(stamps):
        o, h, low, c, v = (
            quote["open"][i],
            quote["high"][i],
            quote["low"][i],
            quote["close"][i],
            quote["volume"][i],
        )
        if None in (o, h, low, c, v):  # Yahoo nulls out non-trading days
            continue
        bars.append(
            Bar(
                symbol=symbol,
                venue=Venue.NSE,
                asset_class=AssetClass.EQUITY,
                start=datetime.fromtimestamp(int(ts), tz=UTC),
                interval=_ONE_DAY,
                open=Decimal(str(o)),
                high=Decimal(str(h)),
                low=Decimal(str(low)),
                close=Decimal(str(c)),
                volume=Decimal(str(int(v))),  # NSE daily volume is whole shares (Yahoo gives ints)
            )
        )
    return bars
