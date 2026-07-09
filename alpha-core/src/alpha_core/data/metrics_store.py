"""Month-partitioned Binance futures METRICS store (the G5 families' signal input).

Columnar 5-minute positioning snapshots from the bulk archive's ``metrics`` tree
(`data.binance.vision /futures/um/daily/metrics/` — the round-3 survey unlock that
REVERSED the M3.0 open-interest ruling): open interest (contracts + USD value), the
top-trader long/short ratios (by accounts and by positions), the global long/short
account ratio, and the taker buy/sell volume ratio. **float64 by design** — positioning
is the statistics plane (signal input, never money). Every column except
``open_interest`` is **NaN where the archive left it blank** (most of 2022 publishes OI
only) — consumers must mask NaN explicitly; only ``open_interest`` is always present.

The ``FlowStore`` patterns verbatim (review-hardened there, #186): month partitions,
atomic glob-invisible tmp writes, idempotent last-wins merges by epoch second, arrow-
native epoch reads (no session-tz layer), and a strictly-ascending span assert.

**Seal posture (TEST-3):** the metrics tree is a signal input to folds driven by the
sealed BAR stores — ``scripts/seal_metrics_store.py`` inherits each series' boundary
from the cold-store holdout manifest (``data_holdout/_windows.json``), so a metrics
value from the holdout era can never sit in the research tree.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from alpha_core.core.enums import Venue

_COLUMNS = (
    "open_interest",
    "open_interest_value",
    "top_ls_accounts",
    "top_ls_positions",
    "global_ls_accounts",
    "taker_buy_sell_ratio",
)
_SCHEMA = pa.schema(
    [("start", pa.timestamp("us", tz="UTC"))] + [(c, pa.float64()) for c in _COLUMNS]
)

# One metrics row: (epoch_seconds, then the six floats in _COLUMNS order).
MetricsRow = tuple[int, float, float, float, float, float, float]


@dataclass(frozen=True, slots=True)
class MetricsSpan:
    """A columnar span: epoch seconds plus the six metric arrays (same order)."""

    epoch_s: np.ndarray  # int64, strictly ascending
    open_interest: np.ndarray
    open_interest_value: np.ndarray
    top_ls_accounts: np.ndarray
    top_ls_positions: np.ndarray
    global_ls_accounts: np.ndarray
    taker_buy_sell_ratio: np.ndarray


def _empty() -> MetricsSpan:
    z = np.array([], dtype=np.float64)
    return MetricsSpan(np.array([], dtype=np.int64), z, z, z, z, z, z)


class MetricsStore:
    """Columnar month-partitioned metrics store (float statistics plane)."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _series_dir(self, venue: Venue, symbol: str) -> Path:
        return self._root / f"{venue.value}__{symbol}__metrics"

    def write_month(self, *, venue: Venue, symbol: str, month: str, rows: list[MetricsRow]) -> int:
        """Merge ``rows`` into the month partition — atomic, idempotent (last wins)."""
        if not rows:
            return 0
        series = self._series_dir(venue, symbol)
        series.mkdir(parents=True, exist_ok=True)
        path = series / f"{month}.parquet"
        merged: dict[int, tuple[float, ...]] = {}
        if path.is_file():
            existing = self._read_file(path)
            for i, e in enumerate(existing.epoch_s):
                merged[int(e)] = tuple(float(getattr(existing, c)[i]) for c in _COLUMNS)
        for row in rows:
            merged[int(row[0])] = tuple(float(v) for v in row[1:])
        ordered = sorted(merged.items())
        arrays: dict[str, pa.Array] = {
            "start": pa.array(
                [datetime.fromtimestamp(e, tz=UTC) for e, _ in ordered],
                type=pa.timestamp("us", tz="UTC"),
            )
        }
        for j, col in enumerate(_COLUMNS):
            arrays[col] = pa.array([vals[j] for _, vals in ordered], type=pa.float64())
        table = pa.table(arrays, schema=_SCHEMA)
        fd, tmp = tempfile.mkstemp(dir=series, prefix=f"{month}.", suffix=".parquet.tmp")
        os.close(fd)
        try:
            pq.write_table(table, tmp)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return len(ordered)

    def months(self, *, venue: Venue, symbol: str) -> list[str]:
        return sorted(f.stem for f in self._series_dir(venue, symbol).glob("*.parquet"))

    def read_span(self, *, venue: Venue, symbol: str) -> MetricsSpan:
        series = self._series_dir(venue, symbol)
        parts = [self._read_file(p) for p in sorted(series.glob("*.parquet"))]
        parts = [p for p in parts if len(p.epoch_s)]
        if not parts:
            return _empty()
        out = MetricsSpan(
            np.concatenate([p.epoch_s for p in parts]),
            *(np.concatenate([getattr(p, c) for p in parts]) for c in _COLUMNS),
        )
        if len(out.epoch_s) > 1 and not bool(np.all(np.diff(out.epoch_s) > 0)):
            raise ValueError("metrics span is not strictly ascending — corrupt partition?")
        return out

    @staticmethod
    def _read_file(path: Path) -> MetricsSpan:
        if not path.is_file():
            return _empty()
        table = pq.read_table(path)
        epoch_us = table.column("start").cast(pa.int64()).to_numpy()
        return MetricsSpan(
            (epoch_us // 1_000_000).astype(np.int64),
            *(table.column(c).to_numpy().astype(np.float64) for c in _COLUMNS),
        )
