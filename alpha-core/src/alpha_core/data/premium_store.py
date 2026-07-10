"""Month-partitioned Binance futures PREMIUM-INDEX store (the G7 family's signal input).

Columnar 1-minute premium-index klines from the bulk archive's ``premiumIndexKlines``
tree (`data.binance.vision /futures/um/monthly/premiumIndexKlines/<SYMBOL>/1m/` — the
round-4 probe's third viable unlock, 2019-12→current): OHLC of the perp-vs-index
premium **as a fraction** (e.g. -0.00023 = -2.3 bps; the tape the funding rate is
computed from). **float64 by design** — the premium is the statistics plane (signal
input, never money). The archive's volume columns are structurally zero and are not
stored. Rows are keyed by the bar's END instant (open_time + 60 s — the decision
instant convention: everything in the bar is printed at/before its end).

The ``MetricsStore``/``DepthStore`` patterns verbatim (review-hardened #186/#189/#194):
month partitions, atomic glob-invisible tmp writes, idempotent last-wins merges by
epoch second, arrow-native epoch reads, and a strictly-ascending span assert.

**Seal posture (TEST-3):** the premium tree is a signal input to folds whose fills and
exits run on the sealed 1s TICK stores — ``scripts/seal_premium_store.py`` inherits
each series' boundary from the tick holdout manifest, so a premium bar from the holdout
era can never sit in the research tree.
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

_COLUMNS = ("premium_open", "premium_high", "premium_low", "premium_close")
_SCHEMA = pa.schema(
    [("start", pa.timestamp("us", tz="UTC"))] + [(c, pa.float64()) for c in _COLUMNS]
)

# One premium row: (bar-END epoch seconds, then OHLC of the premium fraction).
PremiumRow = tuple[int, float, float, float, float]


@dataclass(frozen=True, slots=True)
class PremiumSpan:
    """A columnar span: bar-END epoch seconds plus the four premium arrays."""

    epoch_s: np.ndarray  # int64, strictly ascending — bar END instants
    premium_open: np.ndarray
    premium_high: np.ndarray
    premium_low: np.ndarray
    premium_close: np.ndarray


def _empty() -> PremiumSpan:
    z = np.array([], dtype=np.float64)
    return PremiumSpan(np.array([], dtype=np.int64), z, z, z, z)


class PremiumStore:
    """Columnar month-partitioned premium-index store (float statistics plane)."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _series_dir(self, venue: Venue, symbol: str) -> Path:
        return self._root / f"{venue.value}__{symbol}__premium"

    def write_month(self, *, venue: Venue, symbol: str, month: str, rows: list[PremiumRow]) -> int:
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

    def read_span(self, *, venue: Venue, symbol: str) -> PremiumSpan:
        series = self._series_dir(venue, symbol)
        parts = [self._read_file(p) for p in sorted(series.glob("*.parquet"))]
        parts = [p for p in parts if len(p.epoch_s)]
        if not parts:
            return _empty()
        out = PremiumSpan(
            np.concatenate([p.epoch_s for p in parts]),
            *(np.concatenate([getattr(p, c) for p in parts]) for c in _COLUMNS),
        )
        if len(out.epoch_s) > 1 and not bool(np.all(np.diff(out.epoch_s) > 0)):
            raise ValueError("premium span is not strictly ascending — corrupt partition?")
        return out

    @staticmethod
    def _read_file(path: Path) -> PremiumSpan:
        if not path.is_file():
            return _empty()
        table = pq.read_table(path)
        epoch_us = table.column("start").cast(pa.int64()).to_numpy()
        return PremiumSpan(
            (epoch_us // 1_000_000).astype(np.int64),
            *(table.column(c).to_numpy().astype(np.float64) for c in _COLUMNS),
        )
