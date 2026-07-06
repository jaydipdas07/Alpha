#!/usr/bin/env python
"""Seal the raw flow store by INHERITING the tick store's holdout boundaries (TEST-3).

The flow tree (signed taker volumes — the G2 signal input) spans the same raw era as the
tick store, so it must be partitioned at the SAME per-series boundaries: this script reads
the TICK holdout root's ``_windows.json`` and splits each flow series at its tick twin's
window start — no fraction math of its own, no way to mint a fresher flow window than the
prices already have (the interval-floor discipline, inherited wholesale). A flow series
whose tick twin has no sealed window FAILS LOUD (seal ticks first).

    uv run python scripts/seal_flow_store.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from alpha_core.core.enums import Venue
from alpha_core.data.flow_store import FlowStore
from alpha_core.data.holdout import assert_disjoint_roots

_ROOT = Path(__file__).resolve().parents[1]


def _root(env: str, default: str) -> Path:
    return Path(os.environ.get(env) or (_ROOT / default))


RAW = _root("ALPHA_FLOW_ROOT", "data_cold_flows")
RESEARCH = _root("ALPHA_FLOW_RESEARCH_ROOT", "data_research_flows")
HOLDOUT = _root("ALPHA_FLOW_HOLDOUT_ROOT", "data_holdout_flows")
TICK_HOLDOUT = _root("ALPHA_TICK_HOLDOUT_ROOT", "data_holdout_ticks")


def main() -> int:
    manifest_path = TICK_HOLDOUT / "_windows.json"
    if not manifest_path.is_file():
        print(f"[error] tick seal manifest missing: {manifest_path} — seal ticks first")
        return 1
    manifest = json.loads(manifest_path.read_text())
    raw = FlowStore(RAW)
    research = FlowStore(RESEARCH)
    holdout = FlowStore(HOLDOUT)
    assert_disjoint_roots(research.root, holdout.root)
    assert_disjoint_roots(raw.root, research.root)
    assert_disjoint_roots(raw.root, holdout.root)

    sealed = 0
    for series_dir in sorted(raw.root.glob("*__*__*")):
        venue_s, symbol, interval_s = series_dir.name.split("__")
        venue = Venue(venue_s)
        interval = int(interval_s)
        key = f"{venue_s}|{symbol}|{interval_s}"
        window = manifest.get(key)
        if window is None:
            print(f"[error] no tick seal window for {key} — the flow tree may not out-run ticks")
            return 1
        boundary = datetime.fromisoformat(window["start"]).astimezone(UTC)
        boundary_s = int(boundary.timestamp())
        span = raw.read_span(venue=venue, symbol=symbol, interval_seconds=interval)
        if not len(span.epoch_s):
            continue
        cut = int((span.epoch_s < boundary_s).sum())
        res_rows = list(
            zip(
                span.epoch_s[:cut].tolist(),
                span.buy[:cut].tolist(),
                span.sell[:cut].tolist(),
                strict=True,
            )
        )
        hold_rows = list(
            zip(
                span.epoch_s[cut:].tolist(),
                span.buy[cut:].tolist(),
                span.sell[cut:].tolist(),
                strict=True,
            )
        )
        for store, rows in ((research, res_rows), (holdout, hold_rows)):
            by_month: dict[str, list[tuple[int, float, float]]] = {}
            for e, b, s in rows:
                m = datetime.fromtimestamp(e, tz=UTC)
                by_month.setdefault(f"{m.year:04d}-{m.month:02d}", []).append((e, b, s))
            for month, mrows in by_month.items():
                store.write_month(
                    venue=venue,
                    symbol=symbol,
                    interval_seconds=interval,
                    month=month,
                    rows=mrows,
                )
        print(
            f"  {key}: {len(res_rows)} research + {len(hold_rows)} holdout seconds "
            f"(boundary {boundary.date()} inherited from the tick seal)"
        )
        sealed += 1
    print(f"=== sealed {sealed} flow series — folds read {RESEARCH} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
