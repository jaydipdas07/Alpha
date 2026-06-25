"""B1a.1b — free-data ingest into the Parquet/DuckDB cold store.

Fetches a **fixed past window** of (1) Binance **BTC-perp 5m** and (2) a
**NIFTY-constituent daily** (RELIANCE, via Yahoo), normalizes to core ``Bar``s,
and writes them to the ``BarStore``. The load is **reproducible**: a fixed window
+ the store's idempotent writes mean re-running never duplicates and is byte-stable.

Mac-CLI / network (public endpoints, no keys) — not a CI test. Run:

    uv run python scripts/ingest_cold_store.py
"""

from __future__ import annotations

import json
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alpha_core.core.enums import Venue
from alpha_core.data.ingest.binance import klines_to_bars
from alpha_core.data.ingest.yahoo import chart_to_bars
from alpha_core.data.store import BarStore

STORE_ROOT = Path(__file__).resolve().parents[1] / "data_cold"  # gitignored
_HEADERS = {"User-Agent": "Mozilla/5.0 (alpha-research cold-store ingest)"}


def _get(url: str) -> Any:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _s(dt: datetime) -> int:
    return int(dt.timestamp())


def ingest_binance(store: BarStore) -> int:
    """BTC-perp 5m for a fixed UTC day from Binance's public futures API."""
    start, end = datetime(2026, 6, 20, tzinfo=UTC), datetime(2026, 6, 21, tzinfo=UTC)
    url = (
        "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=5m"
        f"&startTime={_ms(start)}&endTime={_ms(end)}&limit=1500"
    )
    bars = klines_to_bars(_get(url), symbol="BTCUSDT", interval_seconds=300)
    on_disk = store.write_bars(bars)
    print(f"[binance] BTCUSDT 5m {start.date()}: fetched {len(bars)} bars -> {on_disk} on disk")
    return store.write_bars(bars)  # re-write the same window -> idempotent (count unchanged)


def ingest_nse(store: BarStore) -> int:
    """A NIFTY constituent (RELIANCE) daily for a fixed month via Yahoo v8 chart."""
    start, end = datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS"
        f"?period1={_s(start)}&period2={_s(end)}&interval=1d"
    )
    bars = chart_to_bars(_get(url), symbol="NSE:RELIANCE")
    on_disk = store.write_bars(bars)
    print(
        f"[nse]     NSE:RELIANCE 1d {start.date()}..{end.date()}: fetched {len(bars)} -> {on_disk}"
    )
    return store.write_bars(bars)  # idempotent re-write


def main() -> None:
    store = BarStore(STORE_ROOT)
    print(f"=== B1a.1b cold-store ingest -> {STORE_ROOT} ===")
    btc = ingest_binance(store)
    nse = ingest_nse(store)

    # read-back proof + the DuckDB analytical layer over the cold store
    got = store.read_bars(symbol="BTCUSDT", venue=Venue.BINANCE, interval_seconds=300)
    print(
        f"[verify]  BTCUSDT round-trip read: {len(got)} bars (== {btc} on disk: {len(got) == btc})"
    )
    con = store.connect()
    rows = con.execute(
        "SELECT venue, asset_class, count(*) n, min(start) lo, max(start) hi "
        "FROM bars GROUP BY venue, asset_class ORDER BY venue"
    ).fetchall()
    for venue, asset, n, lo, hi in rows:
        print(f"[duckdb]  {venue}/{asset}: {n} bars  {lo} .. {hi}")
    print(f"=== done — {btc + nse} bars in the cold store, reproducible (idempotent re-write) ===")


if __name__ == "__main__":
    main()
