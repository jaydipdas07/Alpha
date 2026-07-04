"""FW-family data leg — Binance archive monthly 1m klines → the classic ``BarStore``.

Long-window 1m history for the funding-window universe (fresh, never-minute-sealed perps).
Monthly ZIPs (~36 per symbol) keep the request count trivial; a month the archive hasn't
published yet (the current/most-recent month) falls back to its daily ZIPs. One
``write_bars`` call per symbol (fresh series ⇒ no merge-rewrite amplification; ~1.6M bars
peak in memory, sequential per symbol). **Avoid re-runs over an existing series**: a re-run
merge-rewrites the whole ~1.6M-bar series through pydantic models (slow, RAM-heavy) — for a
span extension, prefer a fresh ALPHA_COLD_ROOT or accept the one-off cost knowingly.

Run (the FW pre-registered universe, 3y)::

    uv run python scripts/ingest_binance_1m_bulk.py \\
        --symbols LINKUSDT,LTCUSDT,BCHUSDT,ETCUSDT,FILUSDT,ATOMUSDT \\
        --start 2023-07 --end 2026-06

Network glue — not a CI test (``klines_to_bars`` and the store are the tested parts).
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from calendar import monthrange
from datetime import UTC, date, datetime
from pathlib import Path

from alpha_core.core.models import Bar
from alpha_core.data.ingest.binance import klines_to_bars
from alpha_core.data.store import BarStore

_MONTHLY = "https://data.binance.vision/data/futures/um/monthly/klines"
_DAILY = "https://data.binance.vision/data/futures/um/daily/klines"

STORE_ROOT = Path(
    os.environ.get("ALPHA_COLD_ROOT") or (Path(__file__).resolve().parents[1] / "data_cold")
)


def _months(start: str, end: str) -> list[str]:
    y0, m0 = (int(x) for x in start.split("-"))
    y1, m1 = (int(x) for x in end.split("-"))
    out = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _rows(url: str) -> list[list[str]] | None:
    try:
        with urllib.request.urlopen(url, timeout=300) as resp:
            blob = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    with zipfile.ZipFile(io.BytesIO(blob)) as zf, zf.open(zf.namelist()[0]) as f:
        return list(csv.reader(io.TextIOWrapper(f, encoding="utf-8")))


def _month_bars(symbol: str, month: str) -> list[Bar]:
    rows = _rows(f"{_MONTHLY}/{symbol}/1m/{symbol}-1m-{month}.zip")
    if rows is not None:
        return klines_to_bars(rows, symbol=symbol, interval_seconds=60)
    # monthly not published (the archive lags ~days on the newest month) -> daily fallback
    year, mon = (int(x) for x in month.split("-"))
    bars: list[Bar] = []
    for dom in range(1, monthrange(year, mon)[1] + 1):
        day = date(year, mon, dom)
        day_rows = _rows(f"{_DAILY}/{symbol}/1m/{symbol}-1m-{day.isoformat()}.zip")
        if day_rows is None:
            continue
        bars.extend(klines_to_bars(day_rows, symbol=symbol, interval_seconds=60))
        time.sleep(0.1)
    return bars


def main() -> int:
    ap = argparse.ArgumentParser(description="Binance monthly 1m klines -> BarStore")
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--start", required=True, help="YYYY-MM (inclusive)")
    ap.add_argument("--end", required=True, help="YYYY-MM (inclusive)")
    args = ap.parse_args()
    datetime.strptime(args.start + "-01", "%Y-%m-%d").replace(tzinfo=UTC)  # validate
    store = BarStore(STORE_ROOT)
    print(f"=== FW 1m bulk ingest -> {store.root} ===", flush=True)
    grand = 0
    for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        t0 = time.time()
        bars: list[Bar] = []
        for month in _months(args.start, args.end):
            month_bars = _month_bars(symbol, month)
            bars.extend(month_bars)
            print(f"[{symbol}] {month}: {len(month_bars)} bars", flush=True)
            time.sleep(0.1)
        on_disk = store.write_bars(bars)
        grand += len(bars)
        print(
            f"=== {symbol}: {len(bars)} bars -> {on_disk} on disk "
            f"({(time.time() - t0) / 60:.0f} min) ===",
            flush=True,
        )
    print(f"=== done: {grand} bars total ===", flush=True)
    return 0 if grand else 1


if __name__ == "__main__":
    sys.exit(main())
