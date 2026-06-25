"""Normalize raw rows into core ``Tick``/``Bar`` models (ADR 0001/0002).

Raw sources (CSV, adapters) hand us string/scalar rows; these helpers build the
validated core models — Decimal money/qty, tz-aware UTC time — so everything
downstream sees only normalized domain objects.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar, Tick


def _dec(value: Any) -> Decimal | None:
    """Decimal from a non-empty scalar, else None (empty/None/'')."""
    if value is None or value == "":
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _ts(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp; must be tz-aware (Tick/Bar enforce UTC)."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def tick_from_row(row: dict[str, Any]) -> Tick:
    """Build a ``Tick`` from a raw row mapping."""
    return Tick(
        symbol=str(row["symbol"]),
        venue=Venue(row["venue"]),
        asset_class=AssetClass(row["asset_class"]),
        ts=_ts(row["ts"]),
        last_price=_dec(row.get("last_price")),
        last_qty=_dec(row.get("last_qty")),
        bid=_dec(row.get("bid")),
        ask=_dec(row.get("ask")),
        bid_qty=_dec(row.get("bid_qty")),
        ask_qty=_dec(row.get("ask_qty")),
        volume=_dec(row.get("volume")),
    )


def bar_from_row(row: dict[str, Any]) -> Bar:
    """Build a ``Bar`` from a raw row mapping (``interval_seconds`` integer)."""
    return Bar(
        symbol=str(row["symbol"]),
        venue=Venue(row["venue"]),
        asset_class=AssetClass(row["asset_class"]),
        start=_ts(row["start"]),
        interval=timedelta(seconds=int(row["interval_seconds"])),
        open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])),
        low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
        volume=Decimal(str(row["volume"])),
    )
