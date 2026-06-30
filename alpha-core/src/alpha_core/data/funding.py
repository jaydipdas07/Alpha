"""Perp funding-rate history (M3.0 — the funding-carry leg).

Funding is the periodic payment between perpetual-future longs and shorts (every 8h on Binance):
when the rate is positive, longs pay shorts; when negative, shorts pay longs. It is a **cash flow**
— a different return source than price direction, and the closest thing to a structural carry in
crypto. A funding-carry strategy harvests the spread by shorting high-funding names (it receives
the funding) and longing low/negative-funding names (it is paid to hold them).

This module is the **data layer**: a typed ``FundingRate`` record + a Parquet ``FundingStore`` that
mirrors :class:`~alpha_core.data.store.BarStore` (one file per ``(venue, symbol)``, idempotent
dedup-by-time, DuckDB-queryable). Research-plane only (Parquet/DuckDB are dev-group deps; the lean
worker never imports it).

Invariants: the rate is a **signed** ``Decimal`` (it can be negative) stored as native
``DECIMAL(38,18)`` — never a float (B5). ``funding_time`` is tz-aware UTC.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict

from alpha_core.core.enums import Venue
from alpha_core.core.models import Money, UtcDatetime


class FundingRate(BaseModel):
    """One funding payment: the perp ``rate`` charged at ``funding_time`` (a signed Decimal)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    venue: Venue
    funding_time: UtcDatetime
    rate: Money  # signed: > 0 longs pay shorts, < 0 shorts pay longs


_RATE = pa.decimal128(38, 18)  # native DECIMAL — preserves the signed Decimal exactly (no float)
_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("venue", pa.string()),
        ("funding_time", pa.timestamp("us", tz="UTC")),
        ("rate", _RATE),
    ]
)


def _safe(symbol: str) -> str:
    """A reversible, filesystem-safe series key (percent-encode, like ``BarStore._safe``)."""
    return quote(symbol, safe="")


def _row(rate: FundingRate) -> dict[str, Any]:
    return {
        "symbol": rate.symbol,
        "venue": rate.venue.value,
        "funding_time": rate.funding_time,
        "rate": rate.rate,
    }


def _rate(row: dict[str, Any]) -> FundingRate:
    return FundingRate(
        symbol=str(row["symbol"]),
        venue=Venue(row["venue"]),
        funding_time=row["funding_time"],  # pyarrow yields a tz-aware datetime
        rate=row["rate"],  # decimal128 -> Decimal
    )


class FundingStore:
    """A Parquet store of perp funding rates under ``root`` (one file per ``(venue, symbol)``)."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """The store's on-disk root directory (read-only)."""
        return self._root

    def _path(self, venue: Venue, symbol: str) -> Path:
        return self._root / f"{venue.value}__{_safe(symbol)}.parquet"

    def write(self, rates: Sequence[FundingRate]) -> int:
        """Persist ``rates`` (idempotent, dedup by ``funding_time``). Returns the number of distinct
        rates on disk for the touched series after the merge. Re-writing the same is a no-op."""
        groups: dict[tuple[Venue, str], list[FundingRate]] = {}
        for rate in rates:
            groups.setdefault((rate.venue, rate.symbol), []).append(rate)

        written = 0
        for (venue, symbol), group in groups.items():
            path = self._path(venue, symbol)
            merged: dict[datetime, FundingRate] = {}
            if path.exists():
                merged = {r.funding_time: r for r in self._read_file(path)}
            for rate in group:
                merged[rate.funding_time] = rate  # newest wins — dedup by funding_time
            ordered = sorted(merged.values(), key=lambda r: r.funding_time)
            pq.write_table(pa.Table.from_pylist([_row(r) for r in ordered], schema=_SCHEMA), path)
            written += len(ordered)
        return written

    def read(
        self,
        *,
        symbol: str,
        venue: Venue,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[FundingRate]:
        """Read one series back as sorted ``FundingRate`` models over ``[start, end)`` (by time)."""
        path = self._path(venue, symbol)
        if not path.exists():
            return []
        rates = self._read_file(path)
        if start is not None:
            rates = [r for r in rates if r.funding_time >= start]
        if end is not None:
            rates = [r for r in rates if r.funding_time < end]
        return rates

    def _read_file(self, path: Path) -> list[FundingRate]:
        rows: list[dict[str, Any]] = pq.read_table(path).to_pylist()
        return sorted((_rate(r) for r in rows), key=lambda r: r.funding_time)

    def connect(self) -> duckdb.DuckDBPyConnection:
        """A DuckDB connection with a ``funding`` view over every parquet (empty-safe)."""
        con = duckdb.connect()
        files = sorted(self._root.glob("*.parquet"))
        if files:
            globbed = str(self._root / "*.parquet")
            con.execute(f"CREATE VIEW funding AS SELECT * FROM read_parquet('{globbed}')")
        else:
            con.execute(
                "CREATE VIEW funding AS SELECT NULL::VARCHAR AS symbol, NULL::VARCHAR AS venue, "
                "NULL::TIMESTAMPTZ AS funding_time, NULL::DECIMAL(38,18) AS rate WHERE false"
            )
        return con
