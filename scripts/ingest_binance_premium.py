#!/usr/bin/env python
"""Parse the fetched premiumIndexKlines zips into the PremiumStore (G7's signal input).

PURE-LOCAL parse — the raw monthly zips were fetched by the round-4 probe step into
``data_cold_premium/<SYMBOL>/1m/`` (kept on disk; re-parseable). The CSV→row transform
(header sniff, ms/µs stamp guard, bar-END keying, honest bad-row counts) lives
CI-tested in ``alpha_core.data.ingest.binance_premium``; this script only walks zips
and reports. Idempotent per month (store merges last-wins). Run::

    uv run python scripts/ingest_binance_premium.py --symbols BTCUSDT,ETHUSDT
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
from alpha_core.data.ingest.binance_premium import parse_premium_kline_csv
from alpha_core.data.premium_store import PremiumRow, PremiumStore

_ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = Path(os.environ.get("ALPHA_PREMIUM_RAW_ROOT") or (_ROOT / "data_cold_premium"))
STORE_ROOT = Path(os.environ.get("ALPHA_PREMIUM_ROOT") or (_ROOT / "data_cold_premium_store"))


def _parse_zip(path: Path) -> tuple[list[PremiumRow], str]:
    """(rows, report) for one monthly zip; empty/corrupt zips report, never abort."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if not names:
                return [], f"{path.name}: EMPTY ZIP"
            with zf.open(names[0]) as fh:
                rows, stats = parse_premium_kline_csv(io.TextIOWrapper(fh, encoding="utf-8"))
    except zipfile.BadZipFile:
        return [], f"{path.name}: BAD ZIP"
    notes = []
    if stats.bars == 0:
        notes.append("ZERO bars")
    if stats.bad_rows:
        notes.append(f"{stats.bad_rows} bad rows")
    return rows, f"{path.name}: {'; '.join(notes)}" if notes else ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse fetched premium zips into the PremiumStore")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    args = ap.parse_args()

    store = PremiumStore(STORE_ROOT)
    missing: list[str] = []
    for symbol in [s.strip() for s in args.symbols.split(",")]:
        raw_dir = RAW_ROOT / symbol / "1m"
        zips = sorted(raw_dir.glob("*.zip"))
        if not zips:
            print(f"[{symbol}] no zips under {raw_dir} — fetch first")
            missing.append(symbol)
            continue  # keep reporting the symbols that DO have data
        by_month: dict[str, list[PremiumRow]] = {}
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
            f"[{symbol}] {len(zips)} zips -> {total} premium bars across "
            f"{len(months)} months ({months[0]}..{months[-1]}); {len(reports)} file report(s)"
        )
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
