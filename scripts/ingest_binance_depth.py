#!/usr/bin/env python
"""Parse the fetched Binance bookDepth zips into the DepthStore (G6's signal input).

PURE-LOCAL parse — the raw daily zips were fetched by the round-4 probe step into
``data_cold_depth/<SYMBOL>/`` (kept on disk; re-parseable). Twelve rows per ~30 s
snapshot: ``timestamp, percentage, depth, notional`` — the CSV→row transform (band
mapping, honest-NaN presence, last-wins dedup) lives CI-tested in
``alpha_core.data.ingest.binance_depth``; this script only walks zips and reports.

A day whose zip yields zero snapshots is reported; unknown percentage bands are
reported per day (an archive-format change must be visible). Idempotent per month
(store merges last-wins). Run::

    uv run python scripts/ingest_binance_depth.py --symbols BTCUSDT,ETHUSDT
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from alpha_core.core.enums import Venue
from alpha_core.data.depth_store import DepthRow, DepthStore
from alpha_core.data.ingest.binance_depth import parse_book_depth_csv

_ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = Path(os.environ.get("ALPHA_DEPTH_RAW_ROOT") or (_ROOT / "data_cold_depth"))
STORE_ROOT = Path(os.environ.get("ALPHA_DEPTH_ROOT") or (_ROOT / "data_cold_depth_store"))


def _parse_zip(path: Path) -> tuple[list[DepthRow], str]:
    """(rows, report) for one daily zip; empty/corrupt zips report, never abort."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if not names:
                return [], f"{path.name}: EMPTY ZIP"
            with zf.open(names[0]) as fh:
                rows, stats = parse_book_depth_csv(io.TextIOWrapper(fh, encoding="utf-8"))
    except zipfile.BadZipFile:
        return [], f"{path.name}: BAD ZIP"
    notes = []
    if stats.snapshots == 0:
        notes.append("ZERO snapshots")
    if stats.bad_rows:
        notes.append(f"{stats.bad_rows} bad rows")
    if stats.unknown_bands:
        notes.append(f"UNKNOWN bands {sorted(stats.unknown_bands)}")
    return rows, f"{path.name}: {'; '.join(notes)}" if notes else ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse fetched bookDepth zips into the DepthStore")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    args = ap.parse_args()

    store = DepthStore(STORE_ROOT)
    missing: list[str] = []
    for symbol in [s.strip() for s in args.symbols.split(",")]:
        raw_dir = RAW_ROOT / symbol
        zips = sorted(raw_dir.glob("*.zip"))
        if not zips:
            print(f"[{symbol}] no zips under {raw_dir} — fetch first")
            missing.append(symbol)
            continue  # keep reporting the symbols that DO have data
        by_month: dict[str, list[DepthRow]] = {}
        reports: list[str] = []
        for z in zips:
            rows, report = _parse_zip(z)
            if report:
                reports.append(report)
            for row in rows:
                month = datetime.fromtimestamp(row[0], tz=UTC).strftime("%Y-%m")
                by_month.setdefault(month, []).append(row)
        total = 0
        for month in sorted(by_month):
            total += store.write_month(
                venue=Venue.BINANCE, symbol=symbol, month=month, rows=by_month[month]
            )
        for r in reports:
            print(f"[{symbol}] {r}")
        months = sorted(by_month)
        print(
            f"[{symbol}] {len(zips)} zips -> {total} snapshots across "
            f"{len(months)} months ({months[0]}..{months[-1]}); {len(reports)} day report(s)"
        )
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
