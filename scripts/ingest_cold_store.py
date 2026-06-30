"""B1a.1b (+ richer-data) — free-data ingest into the Parquet/DuckDB cold store.

Loads a **richer** research dataset from public endpoints (no keys):

- A **wider Binance USDT-perp universe** (M3.0): 8 top symbols, each at {5m, 1h, 1d}, paginated from
  public futures API (no keys). 1-second bars come from the data.binance.vision archive
  (``scripts/ingest_binance_archive.py``) — a heavier batch, kept separate from this loader.
- The **cross-sectional panels** (M3.0) declared in ``config/discovery.yaml`` — each panel's whole
  member universe at the panel's interval (e.g. the 42-perp ``crypto-perps-1d`` daily panel), so a
  cross-sectional strategy can rank the cross-section. Config-driven (single source of truth); the
  overlap with the single-instrument symbols above is harmlessly re-fetched (idempotent).
- Four **NIFTY-constituent daily** series (RELIANCE, TCS, INFY, HDFCBANK) over ~3 years via Yahoo
  (the Kite 1-minute Indian leg expands this at M3.0).

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

from alpha_core.core.enums import Venue
from alpha_core.core.models import Bar
from alpha_core.data.ingest.binance import klines_to_bars
from alpha_core.data.ingest.yahoo import chart_to_bars
from alpha_core.data.store import BarStore
from alpha_core.helpers.config import load_discovery_config

# The store root: ALPHA_COLD_ROOT, else the repo's gitignored data_cold/.
STORE_ROOT = Path(
    os.environ.get("ALPHA_COLD_ROOT") or (Path(__file__).resolve().parents[1] / "data_cold")
)
_HEADERS = {"User-Agent": "Mozilla/5.0 (alpha-research cold-store ingest)"}

# Fixed past windows (reproducible — a fixed end, never "now").
_END = datetime(2026, 6, 21, tzinfo=UTC)  # recent boundary (covers the B1a.1b seed -> contiguous)
_3Y = datetime(2023, 6, 1, tzinfo=UTC)  # ~3 years of daily history
_1Y = datetime(2025, 6, 21, tzinfo=UTC)  # ~1 year of hourly history
_INTRADAY_START = datetime(2026, 5, 1, tzinfo=UTC)  # ~7 weeks of 5-minute intraday
_1MIN_START = datetime(2026, 5, 31, tzinfo=UTC)  # ~3 weeks of 1-minute (box-safe ~30k bars/cell)

# M3.0 crypto leg — a WIDER free-Binance universe (operator-directed 2026-06-28). Top liquid USDT
# perps, multi-timeframe, from Binance's public futures API (no keys). 1-second bars come from the
# data.binance.vision archive (aggTrades; Binance has no 1s futures klines) via
# scripts/ingest_binance_archive.py — a heavier batch, kept separate from this REST loader.
_CRYPTO_SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "AVAXUSDT",
]
# (binance_interval, interval_seconds, window_start, label) — the REST-ingestable timeframes.
_CRYPTO_TFS = [
    ("1m", 60, _1MIN_START, "1m ~3wk"),
    ("5m", 300, _INTRADAY_START, "5m ~7wk"),
    ("1h", 3600, _1Y, "1h ~1y"),
    ("1d", 86400, _3Y, "1d ~3y"),
]
# interval_seconds -> (binance_interval, window_start) so a cross-sectional panel ingests at the
# same fixed, reproducible window as the matching single-instrument timeframe.
_TF_BY_SECONDS = {seconds: (interval, start) for interval, seconds, start, _ in _CRYPTO_TFS}

# (yahoo_symbol, store_symbol) NIFTY constituents — store symbols match instruments.yaml.
# (The Kite 1-minute Indian leg — kiteconnect + the KITE_ACCESS_TOKEN_AT-staleness reauth — expands
# this universe at M3.0; this free-Yahoo daily set carries equities until then.)
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
    """The wider crypto universe: each ``_CRYPTO_SYMBOLS`` perp at each ``_CRYPTO_TFS`` timeframe,
    paginated from Binance's public futures API (no keys); idempotent (store dedups by start)."""
    total = 0
    for symbol in _CRYPTO_SYMBOLS:
        for interval, seconds, start, label in _CRYPTO_TFS:
            bars = _binance_klines(
                symbol, interval=interval, interval_seconds=seconds, start=start, end=_END
            )
            on_disk = store.write_bars(bars)
            print(f"[binance] {symbol} {label}: fetched {len(bars)} bars -> {on_disk} on disk")
            total += len(bars)
            time.sleep(0.2)  # polite to the public endpoint
    return total


def ingest_panels(store: BarStore) -> int:
    """Ingest the cross-sectional discovery *panels* from config/discovery.yaml: every member
    symbol at the panel's interval, over the same fixed window as the matching single-instrument
    timeframe, via the paginated public futures API (no keys). Idempotent (the store dedups by
    start), so the handful of symbols already covered by ``ingest_binance`` are harmlessly
    re-fetched. This is the free Binance leg, so it ingests only ``venue: BINANCE`` panels (the
    fetch + store path is Binance-specific); other-venue panels come from their own leg (Kite)."""
    total = 0
    for panel in load_discovery_config().panels:
        if panel.venue is not Venue.BINANCE:
            continue  # this loader's fetch/store path is Binance-specific (would mis-store others)
        tf = _TF_BY_SECONDS.get(panel.interval_seconds)
        if tf is None:  # a panel interval with no fixed ingest window — fail loud, never skip
            raise RuntimeError(
                f"panel {panel.name!r}: interval {panel.interval_seconds}s has no ingest window "
                f"(known: {sorted(_TF_BY_SECONDS)})"
            )
        interval, start = tf
        for symbol in panel.symbols:
            bars = _binance_klines(
                symbol,
                interval=interval,
                interval_seconds=panel.interval_seconds,
                start=start,
                end=_END,
            )
            on_disk = store.write_bars(bars)
            print(f"[panel:{panel.name}] {symbol} {interval}: {len(bars)} -> {on_disk} on disk")
            total += len(bars)
            time.sleep(0.2)  # polite to the public endpoint
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
    panels = ingest_panels(store)
    equity = ingest_nse(store)

    # the DuckDB analytical layer over the cold store — one row per series, with span.
    con = store.connect()
    rows = con.execute(
        "SELECT symbol, venue, asset_class, interval_seconds, count(*) n, "
        "min(start) lo, max(start) hi FROM bars GROUP BY 1, 2, 3, 4 ORDER BY 2, 1, 4"
    ).fetchall()
    for symbol, venue, _asset, interval_s, n, lo, hi in rows:
        print(f"[duckdb]  {venue}/{symbol} {interval_s}s: {n} bars  {lo} .. {hi}")
    print(
        f"=== done — {crypto + panels + equity} bars ingested, "
        "reproducible (idempotent re-write) ==="
    )


if __name__ == "__main__":
    main()
