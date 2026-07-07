"""Month-partitioned signed taker-flow store (the G2 family's signal input).

The flow twin of ``tick_store.TickStore``: per (venue, symbol, interval) directory of
monthly parquet partitions holding ``(start, taker_buy_volume, taker_sell_volume)`` —
**float64 columns by design**: flow is the statistics plane (a signal input, never money;
the money path's Decimal discipline lives in the price stores). Reads go through
arrow's tz-aware timestamps cast straight to epoch integers — no session/host-tz layer
exists to mis-render them (the #167 class of bug is structurally absent).

**Seal posture (TEST-3):** the flow tree spans the same raw era as the tick store and
must be partitioned with the SAME boundaries — ``scripts/seal_flow_store.py`` floor-seeds
from the tick seal manifest so a flow value from the holdout era can never sit in the
research tree (volumes correlate with volatility; the strict line costs nothing).
Writes are atomic per month (temp + rename) and idempotent (dedup by ``start``, last wins).
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

_SCHEMA = pa.schema(
    [
        ("start", pa.timestamp("us", tz="UTC")),
        ("taker_buy_volume", pa.float64()),
        ("taker_sell_volume", pa.float64()),
    ]
)


@dataclass(frozen=True, slots=True)
class FlowMonth:
    """One month partition's rows, columnar (epoch seconds + the two flow legs)."""

    epoch_s: np.ndarray  # int64 epoch seconds, ascending
    buy: np.ndarray  # float64 taker-buy volume per bucket
    sell: np.ndarray  # float64 taker-sell volume per bucket


class FlowStore:
    """Columnar month-partitioned flow store (float statistics plane)."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _series_dir(self, venue: Venue, symbol: str, interval_seconds: int) -> Path:
        return self._root / f"{venue.value}__{symbol}__{interval_seconds}"

    def write_month(
        self,
        *,
        venue: Venue,
        symbol: str,
        interval_seconds: int,
        month: str,
        rows: list[tuple[int, float, float]],
    ) -> int:
        """Merge ``rows`` (epoch-seconds, buy, sell) into the month partition — atomic,
        idempotent by ``start`` (last wins), always written time-sorted."""
        if not rows:
            return 0
        series = self._series_dir(venue, symbol, interval_seconds)
        series.mkdir(parents=True, exist_ok=True)
        path = series / f"{month}.parquet"
        merged: dict[int, tuple[float, float]] = {}
        if path.is_file():
            existing = self.read_month(
                venue=venue, symbol=symbol, interval_seconds=interval_seconds, month=month
            )
            for e, b, s in zip(existing.epoch_s, existing.buy, existing.sell, strict=True):
                merged[int(e)] = (float(b), float(s))
        for e, b, s in rows:
            merged[int(e)] = (float(b), float(s))
        ordered = sorted(merged.items())
        table = pa.table(
            {
                "start": pa.array(
                    [datetime.fromtimestamp(e, tz=UTC) for e, _ in ordered],
                    type=pa.timestamp("us", tz="UTC"),
                ),
                "taker_buy_volume": pa.array([bs[0] for _, bs in ordered], type=pa.float64()),
                "taker_sell_volume": pa.array([bs[1] for _, bs in ordered], type=pa.float64()),
            },
            schema=_SCHEMA,
        )
        # tmp name must NOT match the *.parquet glob (a kill between write and replace
        # would otherwise leave an orphan that read_span concatenates mid-sort, #186 F3)
        fd, tmp = tempfile.mkstemp(dir=series, prefix=f"{month}.", suffix=".parquet.tmp")
        os.close(fd)
        try:
            pq.write_table(table, tmp)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return len(ordered)

    def read_month(
        self, *, venue: Venue, symbol: str, interval_seconds: int, month: str
    ) -> FlowMonth:
        path = self._series_dir(venue, symbol, interval_seconds) / f"{month}.parquet"
        return self._read_file(path)

    def read_span(
        self,
        *,
        venue: Venue,
        symbol: str,
        interval_seconds: int,
        months: list[str] | None = None,
    ) -> FlowMonth:
        """Concatenate month partitions (all when ``months`` is None), time-ascending."""
        series = self._series_dir(venue, symbol, interval_seconds)
        if months is None:
            paths = sorted(series.glob("*.parquet"))
        else:
            paths = [series / f"{m}.parquet" for m in months]
        parts = [self._read_file(p) for p in paths]
        parts = [p for p in parts if len(p.epoch_s)]
        if not parts:
            return FlowMonth(
                np.array([], dtype=np.int64),
                np.array([], dtype=np.float64),
                np.array([], dtype=np.float64),
            )
        out = FlowMonth(
            np.concatenate([p.epoch_s for p in parts]),
            np.concatenate([p.buy for p in parts]),
            np.concatenate([p.sell for p in parts]),
        )
        if len(out.epoch_s) > 1 and not bool(np.all(np.diff(out.epoch_s) > 0)):
            raise ValueError("flow span is not strictly ascending — corrupt partition?")
        return out

    def months(self, *, venue: Venue, symbol: str, interval_seconds: int) -> list[str]:
        series = self._series_dir(venue, symbol, interval_seconds)
        return sorted(f.stem for f in series.glob("*.parquet"))

    @staticmethod
    def _read_file(path: Path) -> FlowMonth:
        if not path.is_file():
            return FlowMonth(
                np.array([], dtype=np.int64),
                np.array([], dtype=np.float64),
                np.array([], dtype=np.float64),
            )
        table = pq.read_table(path)
        # arrow tz-aware us-timestamps -> int64 epoch seconds (no host-tz involvement)
        epoch_us = table.column("start").cast(pa.int64()).to_numpy()
        return FlowMonth(
            (epoch_us // 1_000_000).astype(np.int64),
            table.column("taker_buy_volume").to_numpy().astype(np.float64),
            table.column("taker_sell_volume").to_numpy().astype(np.float64),
        )
