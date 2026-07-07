"""G2 flow ingest — Binance archive aggTrades → per-1s SIGNED taker flow → FlowStore.

The signed-flow twin of ``ingest_binance_ticks.py`` (same daily ZIPs, same resumable
month-partition shape): per (symbol, day) download ``data.binance.vision`` aggTrades,
fold to per-second ``(taker_buy_volume, taker_sell_volume)`` via the CI-tested
``aggtrades_to_flows``, buffer per month, write one FlowStore partition per month.
The price/tick store keeps volumes UNSIGNED — this tree exists because the G2 family
needs the aggressor SIDE, which only the raw rows carry.

**Resumable (presence-based):** an interior month WITH a partition is trusted complete
and skipped; the span's edge months always re-merge (idempotent), so an interrupted run
self-heals on the next same-span pass. NB this is WEAKER than the tick twin's per-day
coverage resume: a prior different-span invocation that left a partial interior month
would be masked — keep invocations same-span, and read the per-month report below
(coverage-based resume parity is a tracked follow-up, #186 F1). Mac-only (1s scale).

Run (the G2 registered pair, matching the tick-store span)::

    uv run python scripts/ingest_binance_flows.py \\
        --symbols BTCUSDT,ETHUSDT --start 2024-07-01 --end 2026-06-30

Network glue — not a CI test (the fold + FlowStore are the tested parts).
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
from datetime import date, timedelta
from pathlib import Path

from alpha_core.core.enums import Venue
from alpha_core.data.flow_store import FlowStore
from alpha_core.data.ingest.binance import aggtrades_to_flows

_BASE = "https://data.binance.vision/data/futures/um/daily/aggTrades"
_INTERVAL_SECONDS = 1

STORE_ROOT = Path(
    os.environ.get("ALPHA_FLOW_ROOT") or (Path(__file__).resolve().parents[1] / "data_cold_flows")
)


def _daily_dates(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _download_flows(symbol: str, day: date) -> list[tuple[int, float, float]] | None:
    url = f"{_BASE}/{symbol}/{symbol}-aggTrades-{day.isoformat()}.zip"
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(url, timeout=300) as resp:
                blob = resp.read()
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None  # day not in the archive (weekend gap never happens on perps)
            if attempt == 3:
                raise
            time.sleep(5 * attempt)
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise
            time.sleep(5 * attempt)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as fh:
            reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8"))
            return aggtrades_to_flows(reader, interval_seconds=_INTERVAL_SECONDS)


def main() -> int:
    ap = argparse.ArgumentParser(description="Binance aggTrades -> signed 1s flow store")
    ap.add_argument("--symbols", required=True, help="comma list, e.g. BTCUSDT,ETHUSDT")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    args = ap.parse_args()
    store = FlowStore(STORE_ROOT)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    days = _daily_dates(start, end)
    for symbol in [s.strip() for s in args.symbols.split(",")]:
        done_months = set(
            store.months(venue=Venue.BINANCE, symbol=symbol, interval_seconds=_INTERVAL_SECONDS)
        )
        # Resume rule: interior months with a partition are trusted complete and skipped;
        # the span's EDGE months always re-merge (idempotent) so a partial first/last
        # month from an interrupted run self-heals on the next pass.
        edge_months = {f"{start.year:04d}-{start.month:02d}", f"{end.year:04d}-{end.month:02d}"}
        month_buf: dict[str, list[tuple[int, float, float]]] = {}
        current: str | None = None
        for day in days:
            month = f"{day.year:04d}-{day.month:02d}"
            if month in done_months and month not in edge_months:
                continue
            if current is not None and month != current and current in month_buf:
                rows = month_buf.pop(current)
                n = store.write_month(
                    venue=Venue.BINANCE,
                    symbol=symbol,
                    interval_seconds=_INTERVAL_SECONDS,
                    month=current,
                    rows=rows,
                )
                print(f"[flows] {symbol} {current}: {n} seconds on disk", flush=True)
            current = month
            flows = _download_flows(symbol, day)
            if flows is None:
                print(f"[flows] {symbol} {day}: 404 (skipped)", flush=True)
                continue
            month_buf.setdefault(month, []).extend(flows)
        if current is not None and current in month_buf:
            rows = month_buf.pop(current)
            n = store.write_month(
                venue=Venue.BINANCE,
                symbol=symbol,
                interval_seconds=_INTERVAL_SECONDS,
                month=current,
                rows=rows,
            )
            print(f"[flows] {symbol} {current}: {n} seconds on disk", flush=True)
        report = []
        for m in store.months(
            venue=Venue.BINANCE, symbol=symbol, interval_seconds=_INTERVAL_SECONDS
        ):
            month_rows = store.read_month(
                venue=Venue.BINANCE, symbol=symbol, interval_seconds=_INTERVAL_SECONDS, month=m
            )
            report.append(f"{m}:{len(month_rows.epoch_s)}s")
        print(f"=== {symbol} flow ingest complete — coverage: {' '.join(report)} ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
