#!/usr/bin/env python
"""Build the daily NGE series from the options RESEARCH partition (the NGE-family input).

Runs ``research/nge.py``'s pipeline over ``options_research`` (NEVER the options holdout —
the module docstring's TEST-3 note) and writes the raw per-day series to a small parquet
the ``nge_review`` driver loads. Deterministic; re-runs overwrite. Roots:
``ALPHA_OPTIONS_RESEARCH_ROOT`` (default ``options_research``) → ``ALPHA_NGE_ROOT``
(default ``data_research_nge``). Mac-CLI / no network. Run::

    uv run python scripts/build_nge_series.py [--underlying NIFTY]
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from alpha_core.core.enums import Venue
from alpha_core.data.options_store import OptionsStore
from alpha_core.research.nge import compute_nge_series

_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily NGE series from the options research store")
    ap.add_argument("--underlying", default="NIFTY")
    args = ap.parse_args()

    research_root = Path(
        os.environ.get("ALPHA_OPTIONS_RESEARCH_ROOT") or _ROOT / "options_research"
    )
    out_root = Path(os.environ.get("ALPHA_NGE_ROOT") or _ROOT / "data_research_nge")
    store = OptionsStore(research_root)
    series = compute_nge_series(store, underlying=args.underlying, venue=Venue.NSE)
    if not series:
        print(f"[error] no NGE rows computed for {args.underlying} — wrong root?")
        return 1
    out_root.mkdir(parents=True, exist_ok=True)
    out = out_root / f"NSE__{args.underlying}__nge.parquet"
    table = pa.table(
        {
            "day": pa.array([r.day for r in series], type=pa.timestamp("us", tz="UTC")),
            "nge": pa.array([r.nge for r in series], type=pa.float64()),
            "n_contracts": pa.array([r.n_contracts for r in series], type=pa.int32()),
        }
    )
    pq.write_table(table, out)
    neg = sum(1 for r in series if r.nge < 0)
    print(
        f"=== NGE[{args.underlying}]: {len(series)} days "
        f"({series[0].day.date()} -> {series[-1].day.date()}), "
        f"{neg} negative ({neg / len(series):.1%}) -> {out} ==="
    )
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
