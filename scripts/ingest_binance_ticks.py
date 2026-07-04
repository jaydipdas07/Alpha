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
from alpha_core.data.tick_store import TickStore, month_of

_BASE = "https://data.binance.vision/data/futures/um/daily/aggTrades"
_INTERVAL_SECONDS = 1

STORE_ROOT = Path(
    os.environ.get("ALPHA_TICK_ROOT") or (Path(__file__).resolve().parents[1] / "data_cold_ticks")
)


def _daily_dates(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _download_bars(symbol: str, day: date) -> list[Bar] | None:
    """Daily aggTrades ZIP -> 1s bars, streaming the CSV through the fold (a busy BTC day is
    10-20M rows — only the zip blob and the day's ~86k bars are ever materialized)."""
    url = f"{_BASE}/{symbol}/{symbol}-aggTrades-{day.isoformat()}.zip"
    try:
        with urllib.request.urlopen(url, timeout=300) as resp:
            blob = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # not published (future date / archive gap)
        raise
    with zipfile.ZipFile(io.BytesIO(blob)) as zf, zf.open(zf.namelist()[0]) as f:
        reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8"))
        return aggtrades_to_bars(reader, symbol=symbol, interval_seconds=_INTERVAL_SECONDS)


def _flush(store: TickStore, buffer: list[Bar], symbol: str) -> None:
    """Write the buffer grouped by each bar's OWN month (a rare boundary-spilling row lands
    in its true partition and merges idempotently, instead of wedging the run)."""
    if not buffer:
        return
    by_month: dict[str, list[Bar]] = {}
    for b in buffer:
        by_month.setdefault(month_of(b.start), []).append(b)
    for month, chunk in sorted(by_month.items()):
        t0 = time.time()
        on_disk = store.write_bars(chunk, month=month)
        print(
            f"[{symbol}] month {month}: wrote {len(chunk)} bars -> {on_disk} on disk "
            f"({time.time() - t0:.0f}s)",
            flush=True,
        )


def ingest_symbol(store: TickStore, symbol: str, start: date, end: date) -> int:
    """Stream one symbol's days into monthly partitions, resumable BY MONTH COVERAGE: a day
    is skipped iff its month partition already spans it (min date <= day <= max date). A
    plain series-end marker would be WRONG here — a tail written first (e.g. a smoke run at
    the range end) must not mask an unfilled backfill range before it. Re-fetched boundary
    days merge idempotently (dedup by start)."""
    total = 0
    buffer: list[Bar] = []
    buffer_month: str | None = None
    coverage = {
        m: (lo.date(), hi.date())
        for m, (lo, hi) in store.month_spans(Venue.BINANCE, symbol, _INTERVAL_SECONDS).items()
    }
    for day in _daily_dates(start, end):
        month = f"{day.year:04d}-{day.month:02d}"
        covered = coverage.get(month)
        if covered is not None and covered[0] <= day <= covered[1]:
            continue
        if buffer_month is not None and month != buffer_month:
            _flush(store, buffer, symbol)
            buffer, buffer_month = [], None
        bars = _download_bars(symbol, day)
        if bars is None:
            print(f"[{symbol}] {day}: not published, skipped", flush=True)
            continue
        buffer.extend(bars)
        buffer_month = month
        total += len(bars)
        time.sleep(0.1)  # polite pacing on the public CDN
    if buffer_month is not None:
        _flush(store, buffer, symbol)
    return total


def _coverage_report(store: TickStore, symbol: str, start: date, end: date) -> None:
    """Per-month distinct-day counts vs the requested calendar — an interior 404 hole is
    loud here, never silent (min/max month spans cannot see holes between them)."""
    import duckdb

    d = store.series_dir(Venue.BINANCE, symbol, _INTERVAL_SECONDS)
    if not d.is_dir() or not any(d.glob("*.parquet")):
        print(f"[{symbol}] coverage: NO DATA", flush=True)
        return
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    glob_sql = str(d / "*.parquet").replace("'", "''")
    rows = con.execute(
        f"SELECT strftime(start, '%Y-%m'), count(DISTINCT date_trunc('day', start)) "
        f"FROM read_parquet('{glob_sql}') WHERE start >= ? AND start < ? GROUP BY 1 ORDER BY 1",
        [
            datetime.combine(start, datetime.min.time(), tzinfo=UTC),
            datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC),
        ],
    ).fetchall()
    con.close()
    holes = []
    for month, days in rows:
        y, m = (int(x) for x in month.split("-"))
        from calendar import monthrange

        lo = max(start, date(y, m, 1))
        hi = min(end, date(y, m, monthrange(y, m)[1]))
        expected = (hi - lo).days + 1
        if days < expected:
            holes.append(f"{month}: {days}/{expected} days")
    if holes:
        print(f"[{symbol}] coverage HOLES (archive 404s or gaps): {', '.join(holes)}", flush=True)
    else:
        print(f"[{symbol}] coverage: complete over the requested range", flush=True)


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
        _coverage_report(store, symbol, start, end)
    print(
        f"=== done: {grand} bars total (0 = nothing new; healthy on a complete re-run) ===",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
