"""B1a.1b (+ richer-data) — free-data ingest into the Parquet/DuckDB cold store.

Loads a **richer** research dataset from public endpoints (no keys):

- Binance **BTC-perp 5m** over ~7 weeks (paginated, <=1500 klines/request) + **BTC-perp daily** over
  ~3 years — the intraday and daily crypto series.
- Four **NIFTY-constituent daily** series (RELIANCE, TCS, INFY, HDFCBANK) over ~3 years via Yahoo.

Fixed past windows + the store's idempotent, deterministic writes mean re-running never duplicates
and is byte-stable (reproducibility, B1a.7). The store root defaults to the repo's gitignored
``data_cold/`` but can be overridden with ``ALPHA_COLD_ROOT`` (so the same script loads whichever
cold store the research box / nightly run reads).

Mac-CLI / network — **not a CI test** (the pure transforms in ``data/ingest/`` are the tested part;
this is the network glue). Run:

    uv run python scripts/ingest_cold_store.py

**The cold store this writes is RAW (unsealed).** Before a real 1b.GATE the dataset must be sealed
(``data.holdout.seal_dataset`` — the research/holdout split, B1a.6) and the nightly pointed at the
sealed *research* store, so the discovery loop never sees the rolled-forward holdout (TEST-3/R6).
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from alpha_core.core.models import Bar
from alpha_core.data.ingest.binance import klines_to_bars
from alpha_core.data.ingest.yahoo import chart_to_bars
from alpha_core.data.store import BarStore

# The store root: ALPHA_COLD_ROOT, else the repo's gitignored data_cold/.
STORE_ROOT = Path(
    os.environ.get("ALPHA_COLD_ROOT") or (Path(__file__).resolve().parents[1] / "data_cold")
)
_HEADERS = {"User-Agent": "Mozilla/5.0 (alpha-research cold-store ingest)"}

# Fixed past windows (reproducible — a fixed end, never "now").
_END = datetime(2026, 6, 21, tzinfo=UTC)  # recent boundary (covers the B1a.1b seed -> contiguous)
_3Y = datetime(2023, 6, 1, tzinfo=UTC)  # ~3 years of daily history
_INTRADAY_START = datetime(2026, 5, 1, tzinfo=UTC)  # ~7 weeks of 5-minute intraday

# (yahoo_symbol, store_symbol) NIFTY constituents — store symbols match instruments.yaml.
_EQUITIES = [
    ("RELIANCE.NS", "NSE:RELIANCE"),
    ("TCS.NS", "NSE:TCS"),
    ("INFY.NS", "NSE:INFY"),
    ("HDFCBANK.NS", "NSE:HDFCBANK"),
]


def _get(url: str) -> Any:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _s(dt: datetime) -> int:
    return int(dt.timestamp())


def _binance_klines(
    symbol: str, *, interval: str, interval_seconds: int, start: datetime, end: datetime
) -> list[Bar]:
    """Paginate Binance futures klines over ``[start, end]`` (Binance's ``endTime`` is inclusive, so
    a bar opening exactly at ``end`` is returned) — the API caps a request at 1500 bars, so step the
    cursor past the last bar until the window is covered (or a short page ends it). The store dedups
    by ``start``, so overlapping pages are harmless."""
    bars: list[Bar] = []
    cursor = start
    for _ in range(200):  # hard bound against a non-advancing cursor
        url = (
            f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}"
            f"&startTime={_ms(cursor)}&endTime={_ms(end)}&limit=1500"
        )
        klines = _get(url)
        if not klines:
            break
        bars.extend(klines_to_bars(klines, symbol=symbol, interval_seconds=interval_seconds))
        cursor = datetime.fromtimestamp(int(klines[-1][0]) / 1000, tz=UTC) + timedelta(
            seconds=interval_seconds
        )
        if len(klines) < 1500 or cursor >= end:
            break
        time.sleep(0.2)  # polite to the public endpoint
    else:  # ran the full page cap without finishing -> window too wide; fail loud, never truncate
        raise RuntimeError(f"{symbol} {interval}: window exceeds the 200-page ingest cap")
    return bars


def ingest_binance(store: BarStore) -> int:
    """BTC-perp 5m (~7 weeks, paginated) + BTC-perp daily (~3y) from Binance's public API."""
    total = 0
    for interval, seconds, start, label in (
        ("5m", 300, _INTRADAY_START, "5m ~7wk"),
        ("1d", 86400, _3Y, "1d ~3y"),
    ):
        bars = _binance_klines(
            "BTCUSDT", interval=interval, interval_seconds=seconds, start=start, end=_END
        )
        on_disk = store.write_bars(bars)
        print(f"[binance] BTCUSDT {label}: fetched {len(bars)} bars -> {on_disk} on disk")
        total += len(bars)
    return total


def ingest_nse(store: BarStore) -> int:
    """Four NIFTY constituents, daily ~3y, via the Yahoo v8 chart endpoint."""
    total = 0
    for yahoo_symbol, store_symbol in _EQUITIES:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
            f"?period1={_s(_3Y)}&period2={_s(_END)}&interval=1d"
        )
        bars = chart_to_bars(_get(url), symbol=store_symbol)
        on_disk = store.write_bars(bars)
        print(f"[nse]     {store_symbol} 1d ~3y: fetched {len(bars)} -> {on_disk} on disk")
        total += len(bars)
        time.sleep(0.2)
    return total


def main() -> None:
    store = BarStore(STORE_ROOT)
    print(f"=== cold-store ingest (richer dataset) -> {STORE_ROOT} ===")
    crypto = ingest_binance(store)
    equity = ingest_nse(store)

    # the DuckDB analytical layer over the cold store — one row per series, with span.
    con = store.connect()
    rows = con.execute(
        "SELECT symbol, venue, asset_class, interval_seconds, count(*) n, "
        "min(start) lo, max(start) hi FROM bars GROUP BY 1, 2, 3, 4 ORDER BY 2, 1, 4"
    ).fetchall()
    for symbol, venue, _asset, interval_s, n, lo, hi in rows:
        print(f"[duckdb]  {venue}/{symbol} {interval_s}s: {n} bars  {lo} .. {hi}")
    print(f"=== done — {crypto + equity} bars ingested, reproducible (idempotent re-write) ===")


if __name__ == "__main__":
    main()
