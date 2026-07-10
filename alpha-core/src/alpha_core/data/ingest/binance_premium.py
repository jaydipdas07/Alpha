"""Pure CSV→``PremiumRow`` transform for the Binance ``premiumIndexKlines`` archive (G7).

Standard kline CSV columns (``open_time, open, high, low, close, volume, close_time,
quote_volume, count, taker_buy_volume, taker_buy_quote_volume, ignore``) where OHLC is
the perp-vs-index premium **as a fraction**; the volume columns are structurally zero
and ignored. Two archive quirks handled here (CI-tested):

- **header presence is era-dependent** (older monthlies have no header row) — sniffed
  from the first line;
- ``open_time`` is **milliseconds** in the classic era; a defensive digit-count guard
  also accepts microseconds (Binance migrated some archives to µs) — either way the
  row is keyed by the bar's END second (open + 60 s, the decision-instant convention).

A row with an unparseable stamp or a non-finite premium is counted, never fabricated;
a duplicate ``open_time`` is last-wins. Negative premium is legitimate (backwardation).
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from typing import IO

from alpha_core.data.premium_store import PremiumRow

_BAR_S = 60
# open_time digit guard: >= 1e14 means microseconds (ms stamps are ~1.7e12 this era)
_US_THRESHOLD = 100_000_000_000_000


@dataclass
class PremiumParseStats:
    """What the parse saw — the script reports these per file, honestly."""

    csv_rows: int = 0
    bars: int = 0
    bad_rows: int = 0


def parse_premium_kline_csv(fh: IO[str]) -> tuple[list[PremiumRow], PremiumParseStats]:
    """Parse one premiumIndexKlines CSV (1m) into bar-END-keyed premium rows."""
    stats = PremiumParseStats()
    bars: dict[int, tuple[float, float, float, float]] = {}
    reader = csv.reader(fh)
    for rec in reader:
        if not rec:
            continue
        if rec[0].strip().lower() == "open_time":
            continue  # era-dependent header row
        stats.csv_rows += 1
        try:
            raw = int(rec[0])
            o, h, lo, c = (float(rec[k]) for k in (1, 2, 3, 4))
        except (IndexError, ValueError, TypeError):
            stats.bad_rows += 1
            continue
        if not all(math.isfinite(v) for v in (o, h, lo, c)):
            stats.bad_rows += 1
            continue
        open_s = raw // 1_000_000 if raw >= _US_THRESHOLD else raw // 1_000
        bars[open_s + _BAR_S] = (o, h, lo, c)
    stats.bars = len(bars)
    rows: list[PremiumRow] = [(e, *bars[e]) for e in sorted(bars)]
    return rows, stats
