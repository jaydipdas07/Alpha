#!/usr/bin/env python
"""Refresh the cached Kite instrument master (symbol -> token / lot / tick / expiry).

The worker's kite factory and the ``InstrumentRegistry`` read the cached JSON dump
(``from_kite_json``); this script (re)writes it from the live Kite API. Run it
after the daily 2FA token is staged (scripts/ingest_kite.py) whenever the universe
may have moved — new listings, F&O expiry rollovers (weekly, once options trade
live), symbol changes. The worker auto-fetches when the cache is ABSENT or STALE (the
mtime bound); this script force-refreshes a PRESENT one (delete-and-restart's ops-friendly
sibling). Network glue — the pure parsing lives in ``InstrumentRegistry.from_kite_dump``
(CI-tested); this file is not.

    uv run python scripts/refresh_instruments.py [--cache var/kite_instruments.json]
                                                 [--exchanges NSE NFO]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from alpha_core.execution.instruments import InstrumentRegistry
from worker.config import load_dotenv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", default="var/kite_instruments.json")
    parser.add_argument(
        "--exchanges",
        nargs="+",
        default=["NSE"],
        help="Kite exchange segments to dump (NSE equities; add NFO for index F&O)",
    )
    args = parser.parse_args()

    load_dotenv()
    api_key = os.environ.get("KITE_API_KEY", "")
    access_token = os.environ.get("KITE_ACCESS_TOKEN", "")
    if not api_key or not access_token:
        print("missing KITE_API_KEY / KITE_ACCESS_TOKEN — run scripts/ingest_kite.py first")
        return 2

    from kiteconnect import KiteConnect  # lazy: --help must not need the SDK

    kite = KiteConnect(api_key=api_key, access_token=access_token)
    rows: list[dict[str, object]] = []
    for exchange in args.exchanges:
        chunk = kite.instruments(exchange)
        print(f"{exchange}: {len(chunk)} rows")
        rows.extend(chunk)

    cache = Path(args.cache)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rows, default=str), encoding="utf-8")  # dates -> str
    registry = InstrumentRegistry.from_kite_dump(rows)
    tokens = registry.token_map()
    print(f"wrote {cache} ({len(rows)} rows; {len(tokens)} tradable tokens in the registry)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
