"""Pure CSV→``DepthRow`` transform for the Binance futures ``bookDepth`` archive (G6).

The archive publishes one CSV per day: ``timestamp, percentage, depth, notional`` —
one row per band per ~30-second snapshot (percentage ∈ ±{0.2, 1..5}; negative = the bid
side below the mark, positive = asks above; ``depth``/``notional`` are CUMULATIVE within
the band). Era-probed, corrected in #194's review: the 2023-01 era publishes TEN rows
per snapshot (±1..5 only); the ±0.2 rows first appear 2026-01-16 (twelve thereafter).
Timestamps are naive **UTC** wall stamps.

This module is the CI-tested pure transform; ``scripts/ingest_binance_depth.py`` is the
zip-walking glue. Only ``notional`` (USD) is kept — the registration's pre-committed
side of the archive pair (see ``depth_store``). Presence is honest: a band missing from
a snapshot stays **NaN**; a ``(timestamp, band)`` duplicate is last-wins; a row whose
percentage is not one of the twelve known bands is counted, not silently dropped and
not fatal (an archive-format extension must be visible, not brick the ingest).
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import IO

from alpha_core.data.depth_store import BANDS, DepthRow

_BAND_INDEX = {b: i for i, b in enumerate(BANDS)}
_N = len(BANDS)


@dataclass
class DepthParseStats:
    """What the parse saw — the script reports these per day, honestly."""

    csv_rows: int = 0
    snapshots: int = 0
    bad_rows: int = 0  # unparseable timestamp/percentage/notional
    unknown_bands: set[float] = field(default_factory=set)


def parse_book_depth_csv(fh: IO[str]) -> tuple[list[DepthRow], DepthParseStats]:
    """Parse one daily bookDepth CSV into per-snapshot rows (epoch, 6 bids, 6 asks)."""
    stats = DepthParseStats()
    # epoch -> mutable [bid x 6, ask x 6], NaN until published
    snaps: dict[int, list[float]] = {}
    reader = csv.DictReader(fh)
    for rec in reader:
        stats.csv_rows += 1
        try:
            ts = datetime.strptime(rec["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            pct = float(rec["percentage"])
            notional = float(rec["notional"])
        except (KeyError, ValueError, TypeError):
            stats.bad_rows += 1
            continue
        band = round(abs(pct), 2)
        idx = _BAND_INDEX.get(band)
        if idx is None:
            stats.unknown_bands.add(band)
            continue
        if not math.isfinite(notional) or notional < 0:
            stats.bad_rows += 1
            continue
        epoch = int(ts.timestamp())
        row = snaps.setdefault(epoch, [math.nan] * (2 * _N))
        row[idx if pct < 0 else _N + idx] = notional
    stats.snapshots = len(snaps)
    rows: list[DepthRow] = []
    for epoch in sorted(snaps):
        vals = snaps[epoch]
        rows.append((epoch, *vals))  # type: ignore[arg-type]
    return rows, stats
