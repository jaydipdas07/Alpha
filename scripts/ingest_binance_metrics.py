#!/usr/bin/env python
"""Parse the fetched Binance metrics zips into the MetricsStore (G5's signal input).

PURE-LOCAL parse — the raw daily zips were fetched by the round-3 survey step into
``data_cold_metrics/<SYMBOL>/`` (kept on disk; re-parseable). CSV columns (Binance's
naming is confusing — the mapping is pinned here):

- ``create_time``               naive **UTC** wall stamp of the 5-min snapshot
- ``sum_open_interest``         open interest, CONTRACTS         -> open_interest
- ``sum_open_interest_value``   open interest, USD               -> open_interest_value
- ``count_toptrader_long_short_ratio``  top traders BY ACCOUNTS  -> top_ls_accounts
- ``sum_toptrader_long_short_ratio``    top traders BY POSITIONS -> top_ls_positions
- ``count_long_short_ratio``    global accounts ratio            -> global_ls_accounts
- ``sum_taker_long_short_vol_ratio``    taker buy/sell vol ratio -> taker_buy_sell_ratio

Presence is two-tier: ``create_time`` + both open-interest columns are REQUIRED (a row
missing them is skipped); the four ratio columns are stored as **NaN when absent** — for
most of 2022 the archive publishes OI only, with the ratio fields as empty strings, and
dropping those rows would fabricate a year-long hole in a perfectly good OI series. NaN
is the honest "not published", never interpolated. A day whose zip yields zero rows is
reported. Idempotent per month (store merges last-wins). Run::

    uv run python scripts/ingest_binance_metrics.py --symbols BTCUSDT,ETHUSDT
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import os
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from alpha_core.core.enums import Venue
from alpha_core.data.metrics_store import MetricsRow, MetricsStore

_ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = Path(os.environ.get("ALPHA_METRICS_RAW_ROOT") or (_ROOT / "data_cold_metrics"))
STORE_ROOT = Path(os.environ.get("ALPHA_METRICS_ROOT") or (_ROOT / "data_cold_metrics_store"))


def _ratio(rec: dict[str, str], key: str) -> float:
    """Optional ratio field: NaN when the archive left it blank (the 2022 era)."""
    try:
        return float(rec[key])
    except (KeyError, ValueError, TypeError):
        return math.nan


def _parse_zip(path: Path) -> list[MetricsRow]:
    rows: list[MetricsRow] = []
    with zipfile.ZipFile(path) as zf, zf.open(zf.namelist()[0]) as fh:
        for rec in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8")):
            try:  # timestamp + both OI columns are REQUIRED; ratios degrade to NaN
                ts = datetime.strptime(rec["create_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                oi = float(rec["sum_open_interest"])
                oi_value = float(rec["sum_open_interest_value"])
            except (KeyError, ValueError, TypeError):
                continue  # malformed row — skip, never fabricate
            rows.append(
                (
                    int(ts.timestamp()),
                    oi,
                    oi_value,
                    _ratio(rec, "count_toptrader_long_short_ratio"),
                    _ratio(rec, "sum_toptrader_long_short_ratio"),
                    _ratio(rec, "count_long_short_ratio"),
                    _ratio(rec, "sum_taker_long_short_vol_ratio"),
                )
            )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse fetched metrics zips into the store")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    args = ap.parse_args()
    store = MetricsStore(STORE_ROOT)
    for symbol in [s.strip() for s in args.symbols.split(",")]:
        raw = RAW_ROOT / symbol
        if not raw.is_dir():
            print(f"[error] no raw metrics dir for {symbol}: {raw}")
            return 1
        by_month: dict[str, list[MetricsRow]] = {}
        empty_days = 0
        for zpath in sorted(raw.glob(f"{symbol}-metrics-*.zip")):
            day_rows = _parse_zip(zpath)
            if not day_rows:
                empty_days += 1
                continue
            for row in day_rows:
                m = datetime.fromtimestamp(row[0], tz=UTC)
                by_month.setdefault(f"{m.year:04d}-{m.month:02d}", []).append(row)
        total = 0
        for month in sorted(by_month):
            total += store.write_month(
                venue=Venue.BINANCE, symbol=symbol, month=month, rows=by_month[month]
            )
        print(
            f"=== {symbol}: {total} snapshots across {len(by_month)} months "
            f"({empty_days} empty/unparseable days) -> {store.root} ==="
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
