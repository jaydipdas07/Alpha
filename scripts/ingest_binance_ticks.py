"""F3 tick ingest — Binance archive aggTrades → 1s bars → the month-partitioned TickStore.

The long-window Mac companion to ``ingest_binance_archive.py`` (which targets the classic
``BarStore`` and short windows). Per (symbol, day): download the daily aggTrades ZIP from
``data.binance.vision`` (free, no keys), fold to 1s bars via the CI-tested
``aggtrades_to_bars``, buffer per **month**, and write one TickStore partition per month —
memory stays bounded at ~one month of 1s bars, and no zip is kept on disk.

**Resumable:** a month whose partition already holds bars for its full downloaded day-range is
skipped (partition presence + day coverage via the store's month listing and a cheap span
check); re-running a partial month merges idempotently (dedup by ``start``).

**Mac-only by policy** (TASKS.md infra bullet): the t4g.small worker box OOMs at 1s scale.

Run (F3 pre-registered universe: BTC leader + 5 alt laggards, 24 months)::

    uv run python scripts/ingest_binance_ticks.py \\
        --symbols BTCUSDT,SOLUSDT,DOGEUSDT,XRPUSDT,ADAUSDT,AVAXUSDT \\
        --start 2024-07-01 --end 2026-06-30

Network glue — not a CI test (the CSV→Bar fold and the TickStore are the tested parts).
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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from alpha_core.core.enums import Venue
from alpha_core.core.models import Bar
from alpha_core.data.ingest.binance import aggtrades_to_bars
from alpha_core.data.tick_store import TickStore

_BASE = "https://data.binance.vision/data/futures/um/daily/aggTrades"
_INTERVAL_SECONDS = 1

STORE_ROOT = Path(
    os.environ.get("ALPHA_TICK_ROOT") or (Path(__file__).resolve().parents[1] / "data_cold_ticks")
)


def _daily_dates(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _download_rows(symbol: str, day: date) -> list[list[str]] | None:
    url = f"{_BASE}/{symbol}/{symbol}-aggTrades-{day.isoformat()}.zip"
    try:
        with urllib.request.urlopen(url, timeout=300) as resp:
            blob = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # not published (future date / archive gap)
        raise
    with zipfile.ZipFile(io.BytesIO(blob)) as zf, zf.open(zf.namelist()[0]) as f:
        return list(csv.reader(io.TextIOWrapper(f, encoding="utf-8")))


def _flush(store: TickStore, month: str, buffer: list[Bar], symbol: str) -> None:
    if not buffer:
        return
    t0 = time.time()
    on_disk = store.write_bars(buffer, month=month)
    print(
        f"[{symbol}] month {month}: wrote {len(buffer)} bars -> {on_disk} on disk "
        f"({time.time() - t0:.0f}s)",
        flush=True,
    )


def ingest_symbol(store: TickStore, symbol: str, start: date, end: date) -> int:
    """Stream one symbol's days into monthly partitions, resumable: days strictly before the
    series' last on-disk bar are skipped; the boundary day is re-fetched (a merge is an
    idempotent no-op), so a crash mid-run costs at most one re-downloaded day."""
    total = 0
    buffer: list[Bar] = []
    buffer_month: str | None = None
    span = store.span(Venue.BINANCE, symbol, _INTERVAL_SECONDS)
    resume_from = span[1].date() if span is not None else None
    for day in _daily_dates(start, end):
        if resume_from is not None and day < resume_from:
            continue
        month = f"{day.year:04d}-{day.month:02d}"
        if buffer_month is not None and month != buffer_month:
            _flush(store, buffer_month, buffer, symbol)
            buffer, buffer_month = [], None
        rows = _download_rows(symbol, day)
        if rows is None:
            print(f"[{symbol}] {day}: not published, skipped", flush=True)
            continue
        bars = aggtrades_to_bars(rows, symbol=symbol, interval_seconds=_INTERVAL_SECONDS)
        buffer.extend(bars)
        buffer_month = month
        total += len(bars)
        time.sleep(0.1)  # polite pacing on the public CDN
    if buffer_month is not None:
        _flush(store, buffer_month, buffer, symbol)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="Binance aggTrades -> 1s bars -> TickStore")
    ap.add_argument("--symbols", required=True, help="comma-list, e.g. BTCUSDT,SOLUSDT")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    args = ap.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC).date()
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=UTC).date()
    store = TickStore(STORE_ROOT)
    print(f"=== F3 tick ingest -> {store.root} ===", flush=True)
    grand = 0
    for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        t0 = time.time()
        n = ingest_symbol(store, symbol, start, end)
        grand += n
        print(f"=== {symbol}: {n} bars in {(time.time() - t0) / 60:.0f} min ===", flush=True)
    print(f"=== done: {grand} bars total ===", flush=True)
    return 0 if grand else 1


if __name__ == "__main__":
    sys.exit(main())
