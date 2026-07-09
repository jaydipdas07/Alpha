#!/usr/bin/env python
"""Seal the raw metrics store by INHERITING the 1h bar seal's boundaries (TEST-3).

The metrics tree (5-min OI/positioning snapshots — the G5 signal input) is read by folds
whose decisions happen on the 1h bar tape, so it must be partitioned at the SAME per-symbol
boundaries: this script reads the bar holdout root's ``_windows.json`` and splits each
metrics series at its ``{venue}|{symbol}|3600`` twin's window start — no fraction math of
its own, no way to mint a fresher metrics window than the prices already have (the
interval-floor discipline, inherited wholesale — the ``seal_flow_store`` pattern). A
metrics series whose bar twin has no sealed window FAILS LOUD (seal bars first).

    uv run python scripts/seal_metrics_store.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from alpha_core.core.enums import Venue
from alpha_core.data.holdout import assert_disjoint_roots
from alpha_core.data.metrics_store import MetricsRow, MetricsStore

_ROOT = Path(__file__).resolve().parents[1]
_BAR_INTERVAL_S = 3600


def _root(env: str, default: str) -> Path:
    return Path(os.environ.get(env) or (_ROOT / default))


RAW = _root("ALPHA_METRICS_ROOT", "data_cold_metrics_store")
RESEARCH = _root("ALPHA_METRICS_RESEARCH_ROOT", "data_research_metrics")
HOLDOUT = _root("ALPHA_METRICS_HOLDOUT_ROOT", "data_holdout_metrics")
BAR_HOLDOUT = _root("ALPHA_HOLDOUT_ROOT", "data_holdout")


def main() -> int:
    manifest_path = BAR_HOLDOUT / "_windows.json"
    if not manifest_path.is_file():
        print(f"[error] bar seal manifest missing: {manifest_path} — seal the cold store first")
        return 1
    manifest = json.loads(manifest_path.read_text())
    raw = MetricsStore(RAW)
    research = MetricsStore(RESEARCH)
    holdout = MetricsStore(HOLDOUT)
    assert_disjoint_roots(research.root, holdout.root)
    assert_disjoint_roots(raw.root, research.root)
    assert_disjoint_roots(raw.root, holdout.root)

    sealed = 0
    for series_dir in sorted(raw.root.glob("*__*__metrics")):
        venue_s, symbol, _ = series_dir.name.split("__")
        venue = Venue(venue_s)
        key = f"{venue_s}|{symbol}|{_BAR_INTERVAL_S}"
        window = manifest.get(key)
        if window is None:
            print(f"[error] no 1h bar seal window for {key} — metrics may not out-run the bars")
            return 1
        boundary = datetime.fromisoformat(window["start"]).astimezone(UTC)
        boundary_s = int(boundary.timestamp())
        span = raw.read_span(venue=venue, symbol=symbol)
        if not len(span.epoch_s):
            continue
        cut = int((span.epoch_s < boundary_s).sum())
        cols = (
            span.open_interest,
            span.open_interest_value,
            span.top_ls_accounts,
            span.top_ls_positions,
            span.global_ls_accounts,
            span.taker_buy_sell_ratio,
        )
        all_rows: list[MetricsRow] = [
            (int(e), *(float(c[i]) for c in cols))  # type: ignore[misc]
            for i, e in enumerate(span.epoch_s.tolist())
        ]
        for store, rows in ((research, all_rows[:cut]), (holdout, all_rows[cut:])):
            # REBUILD, never merge (the tick-seal twin's rule): a rolled-forward
            # boundary must not leave stale released-era rows on either side (#186 F2).
            target = store.root / series_dir.name
            if target.is_dir():
                shutil.rmtree(target)
            by_month: dict[str, list[MetricsRow]] = {}
            for row in rows:
                m = datetime.fromtimestamp(row[0], tz=UTC)
                by_month.setdefault(f"{m.year:04d}-{m.month:02d}", []).append(row)
            for month, mrows in by_month.items():
                store.write_month(venue=venue, symbol=symbol, month=month, rows=mrows)
        print(
            f"  {key}: {cut} research + {len(all_rows) - cut} holdout snapshots "
            f"(boundary {boundary.isoformat()} inherited from the 1h bar seal)"
        )
        sealed += 1
    print(f"=== sealed {sealed} metrics series — folds read {RESEARCH} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
