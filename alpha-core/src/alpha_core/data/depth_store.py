"""Month-partitioned Binance futures BOOK-DEPTH store (the G6 family's signal input).

Columnar ~30-second order-book depth snapshots from the bulk archive's ``bookDepth``
tree (`data.binance.vision /futures/um/daily/bookDepth/` — the round-4 probe's viable
unlock): cumulative resting NOTIONAL (USD) within ±0.2 % and ±1..5 % of the mark, both
sides, per snapshot. **float64 by design** — depth is the statistics plane (signal
input, never money). A band the archive left out of a snapshot is **NaN** (honest "not
published", never zero-filled) — consumers must mask NaN explicitly.

Only the USD ``notional`` column is stored: it is the economically meaningful side of
the archive pair and the one the G6 registration pre-commits to. The base-asset
``depth`` column stays in the raw zips under ``data_cold_depth/`` (re-parseable);
switching a fold to it post-hoc would be a NEW registration.

The ``MetricsStore``/``FlowStore`` patterns verbatim (review-hardened there, #186/#189):
month partitions, atomic glob-invisible tmp writes, idempotent last-wins merges by epoch
second, arrow-native epoch reads (no session-tz layer), and a strictly-ascending span
assert.

**Seal posture (TEST-3):** the depth tree is a signal input to folds whose fills and
exits run on the sealed 1s TICK stores — ``scripts/seal_depth_store.py`` inherits each
series' boundary from the tick holdout manifest (``data_holdout_ticks/_windows.json``),
so a depth snapshot from the holdout era can never sit in the research tree.
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

# The archive's percentage bands, by |percentage| from the mark, ascending. Pinned by the
# round-4 probe (both the 2023-01 and 2026-07 eras publish exactly these twelve rows per
# snapshot: ±0.2, ±1..5). Negative percentage = the bid side (below), positive = asks.
BANDS: tuple[float, ...] = (0.2, 1.0, 2.0, 3.0, 4.0, 5.0)

_COLUMNS = tuple(
    f"{side}_{str(b).replace('.', '_')}" for side in ("bid", "ask") for b in BANDS
)  # bid_0_2, bid_1_0, .. bid_5_0, ask_0_2, .. ask_5_0
_SCHEMA = pa.schema(
    [("start", pa.timestamp("us", tz="UTC"))] + [(c, pa.float64()) for c in _COLUMNS]
)

# One depth row: (epoch_seconds, then the twelve notionals in _COLUMNS order).
DepthRow = tuple[
    int, float, float, float, float, float, float, float, float, float, float, float, float
]


@dataclass(frozen=True, slots=True)
class DepthSpan:
    """A columnar span: epoch seconds plus (n, 6) bid/ask notional arrays (BANDS order)."""

    epoch_s: np.ndarray  # int64, strictly ascending
    bid: np.ndarray  # float64, shape (n, len(BANDS)) — cumulative USD notional below mark
    ask: np.ndarray  # float64, shape (n, len(BANDS)) — cumulative USD notional above mark


def _empty() -> DepthSpan:
    return DepthSpan(
        np.array([], dtype=np.int64),
        np.empty((0, len(BANDS)), dtype=np.float64),
        np.empty((0, len(BANDS)), dtype=np.float64),
    )


class DepthStore:
    """Columnar month-partitioned book-depth store (float statistics plane)."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _series_dir(self, venue: Venue, symbol: str) -> Path:
        return self._root / f"{venue.value}__{symbol}__depth"

    def write_month(self, *, venue: Venue, symbol: str, month: str, rows: list[DepthRow]) -> int:
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
                merged[int(e)] = tuple(existing.bid[i]) + tuple(existing.ask[i])
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

    def read_span(self, *, venue: Venue, symbol: str) -> DepthSpan:
        series = self._series_dir(venue, symbol)
        parts = [self._read_file(p) for p in sorted(series.glob("*.parquet"))]
        parts = [p for p in parts if len(p.epoch_s)]
        if not parts:
            return _empty()
        out = DepthSpan(
            np.concatenate([p.epoch_s for p in parts]),
            np.concatenate([p.bid for p in parts]),
            np.concatenate([p.ask for p in parts]),
        )
        if len(out.epoch_s) > 1 and not bool(np.all(np.diff(out.epoch_s) > 0)):
            raise ValueError("depth span is not strictly ascending — corrupt partition?")
        return out

    @staticmethod
    def _read_file(path: Path) -> DepthSpan:
        if not path.is_file():
            return _empty()
        table = pq.read_table(path)
        epoch_us = table.column("start").cast(pa.int64()).to_numpy()
        cols = [table.column(c).to_numpy().astype(np.float64) for c in _COLUMNS]
        k = len(BANDS)
        return DepthSpan(
            (epoch_us // 1_000_000).astype(np.int64),
            np.column_stack(cols[:k]),
            np.column_stack(cols[k:]),
        )
