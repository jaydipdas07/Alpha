"""Parquet + DuckDB cold store for OHLCV bars (B1a.1).

The research-plane cold store. Bars are persisted as per-series **Parquet** files
(one file per ``(venue, symbol, interval)``) with **Decimal-exact** money via
``decimal128`` and tz-aware UTC timestamps, then read back as core ``Bar`` models
or queried analytically with **DuckDB** over the Parquet.

Writes are **idempotent + deterministic**: each write merges with what's on disk,
dedups by bar ``start`` (newest wins), and sorts — so re-ingesting the same window
yields a byte-stable file and never duplicates a bar (reproducibility, B1a.7).

``duckdb``/``pyarrow`` are the ``research`` extra (+ the dev group for CI); the live
worker never imports this module, so it stays out of the execution kernel.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar

# 38 digits / 18 fractional holds crypto + equity prices and volumes exactly; the
# read-back Decimal is value-equal to what was written (scale-normalized to 18).
_MONEY = pa.decimal128(38, 18)
_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("venue", pa.string()),
        ("asset_class", pa.string()),
        ("start", pa.timestamp("us", tz="UTC")),
        ("interval_seconds", pa.int64()),
        ("open", _MONEY),
        ("high", _MONEY),
        ("low", _MONEY),
        ("close", _MONEY),
        ("volume", _MONEY),
    ]
)


def _safe(symbol: str) -> str:
    """A filesystem-safe series key — crypto symbols carry ``/`` and ``:``."""
    return symbol.replace("/", "_").replace(":", "_")


def _row(bar: Bar) -> dict[str, Any]:
    return {
        "symbol": bar.symbol,
        "venue": bar.venue.value,
        "asset_class": bar.asset_class.value,
        "start": bar.start,
        "interval_seconds": int(bar.interval.total_seconds()),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


def _bar(row: dict[str, Any]) -> Bar:
    return Bar(
        symbol=str(row["symbol"]),
        venue=Venue(row["venue"]),
        asset_class=AssetClass(row["asset_class"]),
        start=row["start"],  # pyarrow yields a tz-aware datetime
        interval=timedelta(seconds=int(row["interval_seconds"])),
        open=row["open"],  # decimal128 -> Decimal
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume=row["volume"],
    )


class BarStore:
    """A Parquet cold store of OHLCV bars under ``root``, queryable with DuckDB."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, venue: Venue, symbol: str, interval_seconds: int) -> Path:
        return self._root / f"{venue.value}__{_safe(symbol)}__{interval_seconds}.parquet"

    def write_bars(self, bars: Sequence[Bar]) -> int:
        """Persist ``bars`` (idempotent). Returns the number of distinct bars on disk
        for the touched series after the merge. Re-writing the same bars is a no-op."""
        groups: dict[tuple[Venue, str, int], list[Bar]] = {}
        for bar in bars:
            groups.setdefault(
                (bar.venue, bar.symbol, int(bar.interval.total_seconds())), []
            ).append(bar)

        written = 0
        for (venue, symbol, interval_s), group in groups.items():
            path = self._path(venue, symbol, interval_s)
            merged: dict[datetime, Bar] = {}
            if path.exists():
                merged = {b.start: b for b in self._read_file(path)}
            for bar in group:
                merged[bar.start] = bar  # newest wins — dedup by start
            ordered = sorted(merged.values(), key=lambda b: b.start)
            pq.write_table(pa.Table.from_pylist([_row(b) for b in ordered], schema=_SCHEMA), path)
            written += len(ordered)
        return written

    def read_bars(
        self,
        *,
        symbol: str,
        venue: Venue,
        interval_seconds: int,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        """Read one series back as ``Bar`` models, sorted by start (``[start, end)``)."""
        path = self._path(venue, symbol, interval_seconds)
        if not path.exists():
            return []
        bars = self._read_file(path)
        if start is not None:
            bars = [b for b in bars if b.start >= start]
        if end is not None:
            bars = [b for b in bars if b.start < end]
        return bars

    def _read_file(self, path: Path) -> list[Bar]:
        rows: list[dict[str, Any]] = pq.read_table(path).to_pylist()
        return sorted((_bar(r) for r in rows), key=lambda b: b.start)

    def connect(self) -> duckdb.DuckDBPyConnection:
        """A DuckDB connection with a ``bars`` view over every Parquet file in the
        store — the analytical (warm-query) layer over the cold store."""
        con = duckdb.connect(":memory:")
        # inline the glob (a parameter doesn't bind inside a stored CREATE VIEW); the
        # path is the store root (no user input), single-quotes escaped defensively.
        glob = str(self._root / "*.parquet").replace("'", "''")
        con.execute(f"CREATE VIEW bars AS SELECT * FROM read_parquet('{glob}', union_by_name=true)")
        return con
