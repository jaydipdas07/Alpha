#!/usr/bin/env python
"""Ingest Binance's free public archive (``data.binance.vision``) into the cold store (M3.0).

No API key, no rate limit — bulk daily ZIPs of klines (down to **1-second**) and
tick **aggTrades**, for spot or USDⓈ-M futures. The pure CSV→Bar transforms live in
``alpha_core.data.ingest.binance`` (CI-tested); this is the network glue.

Examples
--------
    # 1-second BTCUSDT perp klines for a week -> cold store
    python scripts/ingest_binance_archive.py --symbol BTCUSDT --interval 1s \
        --market futures --start 2024-06-01 --end 2024-06-07

    # tick aggTrades folded into 1s bars (derive finer-than-kline candles)
    python scripts/ingest_binance_archive.py --symbol BTCUSDT --kind aggTrades \
        --interval 1s --market futures --start 2024-06-01 --end 2024-06-01

Note: Binance publishes 1-second **klines** for *spot* only; for 1-second **perp**
(futures) bars use ``--kind aggTrades --interval 1s`` (tick executions folded to 1s).
Futures klines are 1-minute and coarser.

The cold-store root honors ``ALPHA_COLD_ROOT`` (default ``data_cold``); re-runs are
idempotent (``write_bars`` overwrites the same series window).
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import UTC, date, datetime, timedelta

from alpha_core.data.ingest.binance import aggtrades_to_bars, klines_to_bars
from alpha_core.data.store import BarStore

_BASE = "https://data.binance.vision/data"
_INTERVAL_SECONDS = {"1s": 1, "1m": 60, "3m": 180, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


def _market_path(market: str) -> str:
    return {"futures": "futures/um", "spot": "spot"}[market]


def _daily_dates(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _download_csv_rows(url: str) -> list[list[str]] | None:
    """Fetch a Binance archive ZIP and return its CSV rows, or None if absent (404)."""
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:  # fixed https archive host
            blob = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # that day isn't published (weekend gap / not yet available)
        raise
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as f:
            return list(csv.reader(io.TextIOWrapper(f, encoding="utf-8")))


def _klines_url(market: str, symbol: str, interval: str, day: date) -> str:
    mp = _market_path(market)
    return (
        f"{_BASE}/{mp}/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{day.isoformat()}.zip"
    )


def _aggtrades_url(market: str, symbol: str, day: date) -> str:
    mp = _market_path(market)
    return f"{_BASE}/{mp}/daily/aggTrades/{symbol}/{symbol}-aggTrades-{day.isoformat()}.zip"


def store_symbol_for(symbol: str, market: str) -> str:
    """The cold-store identity for a Binance symbol. Spot and futures are economically distinct
    instruments (different price/funding) sharing ``Venue.BINANCE``, so a bare symbol would collide
    in the cold store (one Parquet per (venue, symbol, interval)). Futures keeps the bare symbol
    (the existing perp convention); spot gets a ``.SPOT`` suffix."""
    return symbol if market == "futures" else f"{symbol}.SPOT"


def ingest_symbol(
    store: BarStore, *, symbol: str, kind: str, interval: str, market: str, start: date, end: date
) -> int:
    """Download the daily archive ZIPs for one symbol over ``[start, end]``, write ``Bar``s, and
    return the bar count. Per-day writes are idempotent (``write_bars`` overwrites the same window),
    so a transient error on a later day never discards earlier days and memory stays bounded over a
    long range. The caller validates the kind/market/interval combo (e.g. no 1s futures klines)."""
    interval_seconds = _INTERVAL_SECONDS[interval]
    store_symbol = store_symbol_for(symbol, market)
    total = 0
    for day in _daily_dates(start, end):
        url = (
            _klines_url(market, symbol, interval, day)
            if kind == "klines"
            else _aggtrades_url(market, symbol, day)
        )
        rows = _download_csv_rows(url)
        if rows is None:
            print(f"[skip] {symbol} {day} not published")
            continue
        bars = (
            klines_to_bars(rows, symbol=store_symbol, interval_seconds=interval_seconds)
            if kind == "klines"
            else aggtrades_to_bars(rows, symbol=store_symbol, interval_seconds=interval_seconds)
        )
        on_disk = store.write_bars(bars)
        total += len(bars)
        print(f"[{symbol} {day}] {kind}: {len(bars)} bars -> {on_disk} on disk")
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest the Binance free archive into the cold store.")
    ap.add_argument("--symbol", required=True, help="e.g. BTCUSDT")
    ap.add_argument("--kind", choices=["klines", "aggTrades"], default="klines")
    ap.add_argument("--interval", default="1s", help="kline interval / aggTrade bar interval")
    ap.add_argument("--market", choices=["futures", "spot"], default="futures")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    args = ap.parse_args()

    if args.interval not in _INTERVAL_SECONDS:
        ap.error(f"unknown interval {args.interval!r}; known: {sorted(_INTERVAL_SECONDS)}")
    if args.market == "futures" and args.kind == "klines" and args.interval == "1s":
        ap.error("Binance has no 1s futures klines; use --kind aggTrades for 1s perp bars")
    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC).date()
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=UTC).date()

    store = BarStore(os.environ.get("ALPHA_COLD_ROOT", "data_cold"))
    total = ingest_symbol(
        store,
        symbol=args.symbol,
        kind=args.kind,
        interval=args.interval,
        market=args.market,
        start=start,
        end=end,
    )
    if total == 0:
        print("no bars ingested (no published days in range)")
        return 1
    store_symbol = store_symbol_for(args.symbol, args.market)
    print(f"=== {store_symbol} {args.kind} {args.interval}: {total} bars -> {store.root} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
