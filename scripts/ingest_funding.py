"""Ingest perp **funding-rate history** into the Parquet funding store (M3.0 funding-carry leg).

Fetches Binance's public ``/fapi/v1/fundingRate`` history (no keys) for every member of the
cross-sectional crypto panels in ``config/discovery.yaml``, over the same fixed, reproducible window
as the daily price bars, and writes it to the funding store (idempotent — re-runs never duplicate).

The store root defaults to the repo-gitignored ``data_funding/`` but is overridable via
``ALPHA_FUNDING_ROOT`` (so the research box / a later seal can point at the right store).

Mac-CLI / network — **not a CI test** (the pure transform in ``data/ingest/binance_funding.py`` is
the tested part; this is the network glue). Run::

    uv run python scripts/ingest_funding.py

**The store this writes is RAW (unsealed).** Before a holdout read the funding must be sealed to a
research / holdout split at the same boundary as the bars (the funding-carry backtest + seal land in
the next increment), so discovery never sees the rolled-forward holdout funding (TEST-3/R6).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.data.funding import FundingRate, FundingStore
from alpha_core.data.ingest.binance_funding import funding_rows_to_rates
from alpha_core.helpers.config import load_discovery_config

STORE_ROOT = Path(
    os.environ.get("ALPHA_FUNDING_ROOT") or (Path(__file__).resolve().parents[1] / "data_funding")
)
_HEADERS = {"User-Agent": "Mozilla/5.0 (alpha-research funding ingest)"}

# Fixed past window (reproducible — a fixed end, never "now"), matching the daily price bars.
_START = datetime(2023, 6, 1, tzinfo=UTC)
_END = datetime(2026, 6, 21, tzinfo=UTC)


def _get(url: str) -> Any:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def fetch_funding(symbol: str, *, start: datetime, end: datetime) -> list[FundingRate]:
    """Paginate Binance funding history over ``[start, end]`` (ascending; the API caps a request at
    1000 rows, so step the cursor past the last row until the window is covered)."""
    rates: list[FundingRate] = []
    cursor = start
    for _ in range(200):  # hard bound against a non-advancing cursor
        url = (
            f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}"
            f"&startTime={_ms(cursor)}&endTime={_ms(end)}&limit=1000"
        )
        rows = _get(url)
        if not rows:
            break
        rates.extend(funding_rows_to_rates(rows, symbol=symbol))
        last = datetime.fromtimestamp(int(rows[-1]["fundingTime"]) / 1000, tz=UTC)
        cursor = datetime.fromtimestamp(last.timestamp() + 1, tz=UTC)  # 1s past the last row
        if len(rows) < 1000 or cursor >= end:
            break
        time.sleep(0.2)  # polite to the public endpoint
    else:  # ran the full page cap without finishing -> window too wide; fail loud, never truncate
        raise RuntimeError(f"{symbol}: funding window exceeds the 200-page ingest cap")
    return rates


def _panel_symbols() -> list[str]:
    """The distinct member symbols of the crypto panels (the funding-carry universe)."""
    seen: dict[str, None] = {}
    for panel in load_discovery_config().panels:
        if panel.venue is Venue.BINANCE and panel.market is AssetClass.CRYPTO:
            for symbol in panel.symbols:
                seen.setdefault(symbol, None)
    return list(seen)


def main() -> None:
    store = FundingStore(STORE_ROOT)
    symbols = _panel_symbols()
    print(f"=== funding ingest ({len(symbols)} perps) -> {STORE_ROOT} ===")
    total = 0
    for symbol in symbols:
        try:
            rates = fetch_funding(symbol, start=_START, end=_END)
        except (urllib.error.URLError, RuntimeError) as exc:
            # a delisted/invalid member (HTTP 400) or page-cap must not abort the whole 42-name run.
            print(f"[funding] {symbol}: SKIPPED ({type(exc).__name__}: {exc})")
            continue
        on_disk = store.write(rates)
        print(f"[funding] {symbol}: fetched {len(rates)} -> {on_disk} on disk")
        total += len(rates)
        time.sleep(0.2)  # polite to the public endpoint
    print(f"=== done — {total} funding rows ingested, reproducible (idempotent re-write) ===")


if __name__ == "__main__":
    main()
