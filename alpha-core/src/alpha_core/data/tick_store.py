"""Month-partitioned Parquet store for tick-scale (1s-class) bar series (F3, the lead-lag
family — `docs/research/intraday-edge-survey-2026-07.md` §2.3).

Why not ``BarStore``: its ``write_bars`` merge-rewrites one Parquet per series **through
pydantic ``Bar`` models in a Python dict** — perfect at daily/hourly scale, catastrophic at
1-second scale (a 60M-row series would materialize tens of GB of model objects per write; this
is exactly how the t4g.small research attempt died). ``TickStore`` therefore:

- partitions each series into **one Parquet per month** (`{VENUE}__{SYMBOL}__{interval}/
  {YYYY-MM}.parquet`, ~2.7M rows for 1s) — writes touch one month, never the series;
- moves data **columnar-in, columnar-out** on the READ path (DuckDB, UTC-pinned) — the
  vectorized folds want arrays; the write-merge round-trips at most ONE month through models;
- keeps the ``BarStore`` schema (same columns, Decimal money per B5) so the two stores stay
  mutually legible, and dedups **within a month by ``start``** (newest wins, idempotent).

Research-plane only. The seal (research/holdout physical split, TEST-3) is a separate script
over month files — cheap file moves plus one row-split of the boundary month.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar

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
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def month_of(instant: datetime) -> str:
    """The ``YYYY-MM`` partition key of a tz-aware UTC instant."""
    if instant.tzinfo is None:
        raise ValueError("instant must be tz-aware")
    utc = instant.astimezone(UTC)
    return f"{utc.year:04d}-{utc.month:02d}"


class TickStore:
    """Month-partitioned Parquet bar store for tick-scale series (see module docstring)."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def series_dir(self, venue: Venue, symbol: str, interval_seconds: int) -> Path:
        return self._root / f"{venue.value}__{quote(symbol, safe='')}__{interval_seconds}"

    def _month_path(self, venue: Venue, symbol: str, interval_seconds: int, month: str) -> Path:
        if not _MONTH_RE.match(month):
            raise ValueError(f"month must be 'YYYY-MM', got {month!r}")
        return self.series_dir(venue, symbol, interval_seconds) / f"{month}.parquet"

    def write_bars(self, bars: list[Bar], *, month: str) -> int:
        """Write ``bars`` (all belonging to ``month``, one series) into that month's partition,
        merging with anything already there (dedup by ``start``, newest wins — idempotent).
        Returns the distinct bar count on disk for the month. Bounded by one month's rows."""
        if not bars:
            return 0
        first = bars[0]
        venue, symbol = first.venue, first.symbol
        interval_s = int(first.interval.total_seconds())
        merged: dict[datetime, Bar] = {}
        path = self._month_path(venue, symbol, interval_s, month)
        if path.exists():
            for b in self._read_month(path):
                merged[b.start] = b
        for b in bars:
            if (b.venue, b.symbol, int(b.interval.total_seconds())) != (venue, symbol, interval_s):
                raise ValueError("write_bars: all bars must belong to one series")
            if month_of(b.start) != month:
                raise ValueError(f"bar start {b.start} outside partition month {month}")
            merged[b.start] = b
        ordered = sorted(merged.values(), key=lambda b: b.start)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        table = pa.Table.from_pylist(
            [
                {
                    "symbol": b.symbol,
                    "venue": b.venue.value,
                    "asset_class": b.asset_class.value,
                    "start": b.start,
                    "interval_seconds": int(b.interval.total_seconds()),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
                for b in ordered
            ],
            schema=_SCHEMA,
        )
        pq.write_table(table, tmp)
        os.replace(tmp, path)  # atomic: a kill mid-write can never leave a torn partition
        return len(ordered)

    def _read_month(self, path: Path) -> list[Bar]:
        table = pq.read_table(path)
        rows = table.to_pylist()
        return [
            Bar(
                symbol=r["symbol"],
                venue=Venue(r["venue"]),
                asset_class=AssetClass(r["asset_class"]),
                start=r["start"],
                interval=timedelta(seconds=r["interval_seconds"]),
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
            )
            for r in rows
        ]

    def months(self, venue: Venue, symbol: str, interval_seconds: int) -> list[str]:
        """The sorted month partitions present for a series."""
        d = self.series_dir(venue, symbol, interval_seconds)
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.parquet"))

    def read_columns(
        self,
        *,
        venue: Venue,
        symbol: str,
        interval_seconds: int,
        columns: tuple[str, ...] = ("start", "close", "volume"),
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pa.Table:
        """Columnar read of one series over ``[start, end)``, ordered by ``start`` — the
        vectorized research folds' input (no per-row models)."""
        for bound in (start, end):
            if bound is not None and bound.tzinfo is None:
                raise ValueError("start/end must be tz-aware (naive would be read as host-local)")
        d = self.series_dir(venue, symbol, interval_seconds)
        if not d.is_dir() or not any(d.glob("*.parquet")):
            return _SCHEMA.empty_table().select(list(columns))
        con = duckdb.connect()
        con.execute("SET TimeZone='UTC'")  # host-tz sessions would re-render every timestamp
        cols = ", ".join(f'"{c}"' for c in columns)
        clauses = []
        params: list[object] = []
        if start is not None:
            clauses.append("start >= ?")
            params.append(start.astimezone(UTC))
        if end is not None:
            clauses.append("start < ?")
            params.append(end.astimezone(UTC))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        glob_sql = str(d / "*.parquet").replace("'", "''")
        result = con.execute(
            f"SELECT {cols} FROM read_parquet('{glob_sql}') {where} ORDER BY start", params
        ).to_arrow_table()
        con.close()
        return result

    def month_spans(
        self, venue: Venue, symbol: str, interval_seconds: int
    ) -> dict[str, tuple[datetime, datetime]]:
        """Per existing month partition: ``{month: (min start, max start)}`` — the ingest's
        resume map (day-level coverage, not a single series-end marker: a series whose tail
        was written first — e.g. a smoke test — must not mask an unfilled backfill range)."""
        d = self.series_dir(venue, symbol, interval_seconds)
        if not d.is_dir() or not any(d.glob("*.parquet")):
            return {}
        con = duckdb.connect()
        glob_sql = str(d / "*.parquet").replace("'", "''")
        rows = con.execute(
            f"SELECT strftime(start AT TIME ZONE 'UTC', '%Y-%m') m, min(start), max(start) "
            f"FROM read_parquet('{glob_sql}') GROUP BY 1"
        ).fetchall()
        con.close()
        return {m: (lo.astimezone(UTC), hi.astimezone(UTC)) for m, lo, hi in rows}

    def span(
        self, venue: Venue, symbol: str, interval_seconds: int
    ) -> tuple[datetime, datetime] | None:
        """(min start, max start) of a series, or None when empty."""
        d = self.series_dir(venue, symbol, interval_seconds)
        if not d.is_dir() or not any(d.glob("*.parquet")):
            return None
        con = duckdb.connect()
        glob_sql = str(d / "*.parquet").replace("'", "''")
        lo, hi = con.execute(
            f"SELECT min(start), max(start) FROM read_parquet('{glob_sql}')"
        ).fetchone()
        con.close()
        return (lo.astimezone(UTC), hi.astimezone(UTC))
