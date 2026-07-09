#!/usr/bin/env python
"""Seal the raw depth store by INHERITING the tick store's holdout boundaries (TEST-3).

The depth tree (book-depth notionals — the G6 signal input) feeds folds whose fills and
exits run on the 1s TICK tape, so it must be partitioned at the SAME per-series
boundaries: this script reads the TICK holdout root's ``_windows.json`` and splits each
depth series at its tick twin's window start — no fraction math of its own, no way to
mint a fresher depth window than the prices already have (the interval-floor discipline,
inherited wholesale — the ``seal_flow_store.py`` pattern verbatim). A depth series whose
tick twin has no sealed window FAILS LOUD (seal ticks first).

    uv run python scripts/seal_depth_store.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from alpha_core.core.enums import Venue
from alpha_core.data.depth_store import DepthRow, DepthStore
from alpha_core.data.holdout import assert_disjoint_roots

_ROOT = Path(__file__).resolve().parents[1]

# The depth fold decides at snapshot instants but FILLS on the 1s tape — the tick seal
# is the binding fence, keyed by the tick series' interval.
_TICK_INTERVAL_S = 1


def _root(env: str, default: str) -> Path:
    return Path(os.environ.get(env) or (_ROOT / default))


RAW = _root("ALPHA_DEPTH_ROOT", "data_cold_depth_store")
RESEARCH = _root("ALPHA_DEPTH_RESEARCH_ROOT", "data_research_depth")
HOLDOUT = _root("ALPHA_DEPTH_HOLDOUT_ROOT", "data_holdout_depth")
TICK_HOLDOUT = _root("ALPHA_TICK_HOLDOUT_ROOT", "data_holdout_ticks")


def main() -> int:
    manifest_path = TICK_HOLDOUT / "_windows.json"
    if not manifest_path.is_file():
        print(f"[error] tick seal manifest missing: {manifest_path} — seal ticks first")
        return 1
    manifest = json.loads(manifest_path.read_text())
    raw = DepthStore(RAW)
    research = DepthStore(RESEARCH)
    holdout = DepthStore(HOLDOUT)
    assert_disjoint_roots(research.root, holdout.root)
    assert_disjoint_roots(raw.root, research.root)
    assert_disjoint_roots(raw.root, holdout.root)

    sealed = 0
    for series_dir in sorted(raw.root.glob("*__*__depth")):
        venue_s, symbol, _suffix = series_dir.name.split("__")
        venue = Venue(venue_s)
        key = f"{venue_s}|{symbol}|{_TICK_INTERVAL_S}"
        window = manifest.get(key)
        if window is None:
            print(f"[error] no tick window for {key} — the depth tree may not out-run ticks")
            return 1
        boundary = datetime.fromisoformat(window["start"]).astimezone(UTC)
        boundary_s = int(boundary.timestamp())
        span = raw.read_span(venue=venue, symbol=symbol)
        if not len(span.epoch_s):
            print(f"[skip] {series_dir.name}: empty raw series")
            continue
        cut = int((span.epoch_s < boundary_s).sum())

        # REBUILD both sides, never merge: a rolled-forward boundary must not leave
        # stale released-era rows on either side (#186 F2).
        for store, lo, hi in ((research, 0, cut), (holdout, cut, len(span.epoch_s))):
            target = store.root / series_dir.name
            if target.exists():
                shutil.rmtree(target)
            by_month: dict[str, list[DepthRow]] = {}
            for i in range(lo, hi):
                e = int(span.epoch_s[i])
                month = datetime.fromtimestamp(e, tz=UTC).strftime("%Y-%m")
                row: DepthRow = (e, *span.bid[i].tolist(), *span.ask[i].tolist())
                by_month.setdefault(month, []).append(row)
            for month, rows in sorted(by_month.items()):
                store.write_month(venue=venue, symbol=symbol, month=month, rows=rows)
        print(
            f"[sealed] {series_dir.name}: boundary {boundary.isoformat()} — "
            f"{cut} research + {len(span.epoch_s) - cut} holdout snapshots"
        )
        sealed += 1
    if not sealed:
        print("[error] no raw depth series found — run the depth ingest first")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
