"""F4 archive leg — Binance cm liquidationSnapshot daily ZIPs → one Parquet per contract.

The ONLY archived liquidation history Binance publishes: **coin-margined** perps,
**2023-06-25 → 2024-10-14** (verified against the S3 listing; um was never archived and the
cm series stopped). That window ends before any sensible holdout — so this data feeds the
EXPLORATORY prior study only (``scripts/liquidation_explore.py``); the gate-eligible family
runs on the forward um recorder (``scripts/record_liquidations.py``).

Tiny data (dozens-to-hundreds of events/day): everything buffers in memory and writes one
Parquet per contract under ``data_liq/``.

    uv run python scripts/ingest_binance_liquidations.py \\
        --contracts BTCUSD_PERP,ETHUSD_PERP --start 2023-06-25 --end 2024-10-14
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

import pyarrow as pa
import pyarrow.parquet as pq

_BASE = "https://data.binance.vision/data/futures/cm/daily/liquidationSnapshot"
OUT_ROOT = Path(
    os.environ.get("ALPHA_LIQ_ROOT") or (Path(__file__).resolve().parents[1] / "data_liq")
)
_SCHEMA = pa.schema(
    [
        ("contract", pa.string()),
        ("ts", pa.timestamp("us", tz="UTC")),
        ("side", pa.string()),  # BUY = shorts force-closed; SELL = longs force-closed
        ("quantity", pa.float64()),  # contracts (cm: fixed USD multiple per contract)
        ("price", pa.float64()),
    ]
)


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _rows(url: str) -> list[list[str]] | None:
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            blob = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    with zipfile.ZipFile(io.BytesIO(blob)) as zf, zf.open(zf.namelist()[0]) as f:
        return list(csv.reader(io.TextIOWrapper(f, encoding="utf-8")))


def main() -> int:
    ap = argparse.ArgumentParser(description="cm liquidationSnapshot -> data_liq parquet")
    ap.add_argument("--contracts", default="BTCUSD_PERP,ETHUSD_PERP")
    ap.add_argument("--start", default="2023-06-25")
    ap.add_argument("--end", default="2024-10-14")
    args = ap.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC).date()
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=UTC).date()
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    for contract in [c.strip() for c in args.contracts.split(",") if c.strip()]:
        events: list[dict[str, object]] = []
        missing = 0
        for day in _days(start, end):
            rows = _rows(f"{_BASE}/{contract}/{contract}-liquidationSnapshot-{day.isoformat()}.zip")
            if rows is None:
                missing += 1
                continue
            header, *data = rows
            idx = {name: i for i, name in enumerate(header)}
            for r in data:
                events.append(
                    {
                        "contract": contract,
                        "ts": datetime.fromtimestamp(int(r[idx["time"]]) / 1000, tz=UTC),
                        "side": r[idx["side"]],
                        "quantity": float(r[idx["accumulated_fill_quantity"]]),
                        "price": float(r[idx["average_price"]]),
                    }
                )
            time.sleep(0.05)
        events.sort(key=lambda e: e["ts"])  # type: ignore[arg-type, return-value]
        table = pa.Table.from_pylist(events, schema=_SCHEMA)
        path = OUT_ROOT / f"{contract}.parquet"
        pq.write_table(table, path)
        print(
            f"=== {contract}: {len(events)} events -> {path} ({missing} unpublished days) ===",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
