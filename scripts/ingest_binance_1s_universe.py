#!/usr/bin/env python
"""Drive 1-second perp-bar ingest over the crypto discovery universe (M3.0 1-second leg).

A thin universe driver over ``scripts/ingest_binance_archive.py``: for each distinct crypto symbol
in ``config/discovery.yaml`` (the ``BINANCE``-venue cells), download the ``data.binance.vision``
**aggTrades** archive and fold it to **1-second** perp ``Bar``s — Binance publishes no 1-second
*futures* klines, so 1s perp bars are derived from tick aggTrades — then write them to the cold
store. Reuses ``ingest_symbol`` from the single-symbol script so the network/transform path can't
drift between the two.

The universe defaults to the crypto symbols already in the discovery universe, so it tracks
``config/discovery.yaml`` automatically (override with ``--symbols``). 1s tick data is large, so the
heavy archive RUN is the research box's job over a focused window — this is the code:

    uv run python scripts/ingest_binance_1s_universe.py --start 2026-06-14 --end 2026-06-21
    uv run python scripts/ingest_binance_1s_universe.py --symbols BTCUSDT ETHUSDT \
        --start 2026-06-14 --end 2026-06-21

The store root honors ``ALPHA_COLD_ROOT``; re-runs are idempotent (``write_bars`` overwrites the
same series window). Mac-CLI / network — **not a CI test** (the pure transforms in
``data/ingest/binance.py`` are the tested part; this is the network glue).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.data.store import BarStore
from alpha_core.helpers.config import load_discovery_config

# Reuse ingest_symbol from the sibling single-symbol script. A direct `python scripts/x.py` puts
# scripts/ on sys.path; an importlib / `-m` / test load may not — make it robust either way.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ingest_binance_archive import ingest_symbol  # sibling script (path-bootstrapped above)


def crypto_universe() -> list[str]:
    """The distinct Binance crypto symbols in the discovery universe (sorted, deduped).

    Reads ``config/discovery.yaml`` so the 1s ingest always covers exactly the symbols the sweep
    cares about — adding a crypto symbol to the universe automatically extends this ingest."""
    cells = load_discovery_config().cells
    return sorted(
        {
            cell.symbol
            for cell in cells
            if cell.market is AssetClass.CRYPTO and cell.venue is Venue.BINANCE
        }
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest 1-second Binance perp bars over the universe.")
    ap.add_argument(
        "--symbols", nargs="*", help="symbols (default: crypto cells in discovery.yaml)"
    )
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC).date()
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=UTC).date()
    if start > end:
        print(f"[error] --start {start} is after --end {end}", file=sys.stderr)
        return 1
    symbols = args.symbols or crypto_universe()
    if not symbols:
        print("[error] no crypto symbols (check discovery.yaml or pass --symbols)", file=sys.stderr)
        return 1

    store = BarStore(os.environ.get("ALPHA_COLD_ROOT", "data_cold"))
    print(
        f"=== Binance 1s aggTrades {start}..{end} over {len(symbols)} symbols -> {store.root} ==="
    )
    grand = 0
    for symbol in symbols:
        # 1s perp bars come from aggTrades (no 1s futures klines), interval 1s, futures market.
        total = ingest_symbol(
            store,
            symbol=symbol,
            kind="aggTrades",
            interval="1s",
            market="futures",
            start=start,
            end=end,
        )
        print(f"[{symbol}] 1s total: {total} bars")
        grand += total

    if grand == 0:
        print("no bars ingested (no published days in range)")
        return 1
    print(f"=== done — {grand} 1s bars over {len(symbols)} symbols -> {store.root} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
