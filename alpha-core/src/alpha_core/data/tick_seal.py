"""Seal the raw tick store into research + holdout tick stores (TEST-3 at 1s scale).

The exact ``seal_cold_store`` semantics — per-series roll-forward window
(``rigor.yaml holdout.fraction`` of the span) pinned by the **monotonic floor**
(``floored_window`` over the holdout root's ``_windows.json``) — implemented over month
partitions instead of whole-series rewrites: months strictly before the boundary month are
copied to the research root, months after to the holdout root, and the boundary month is
row-split once. Physical separation, same manifest format, same floor helpers — one
discipline, two scales.

Re-sealing a series REBUILDS both sides from the raw store (stale partitions removed first):
the research side must never retain a bar that the rolled-forward window now claims, and the
holdout side must be exactly the current window (the ``seal_dataset`` rationale).
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from urllib.parse import unquote

import pyarrow.compute as pc
import pyarrow.parquet as pq

from alpha_core.core.enums import Venue
from alpha_core.data.holdout import (
    HoldoutWindow,
    assert_disjoint_roots,
    compute_holdout_window,
    floored_window,
    prior_window_starts,
    write_seal_manifest,
)
from alpha_core.data.tick_store import TickStore, month_of


def _series_coords(store: TickStore) -> list[tuple[Venue, str, int]]:
    """(venue, symbol, interval_seconds) for every series directory in a tick store."""
    out = []
    for d in sorted(p for p in store.root.iterdir() if p.is_dir()):
        venue_s, symbol_q, interval_s = d.name.rsplit("__", 2)
        out.append((Venue(venue_s), unquote(symbol_q), int(interval_s)))
    return out


def seal_tick_store(
    raw: TickStore, research: TickStore, holdout: TickStore, *, fraction: float
) -> dict[str, HoldoutWindow]:
    """Split every raw series into research/holdout tick stores; returns the per-series
    locked windows (also written to the holdout root's ``_windows.json``)."""
    assert_disjoint_roots(research.root, holdout.root)
    assert_disjoint_roots(raw.root, research.root)
    assert_disjoint_roots(raw.root, holdout.root)
    prior = prior_window_starts(holdout.root)
    windows: dict[str, HoldoutWindow] = {}
    for venue, symbol, interval_s in _series_coords(raw):
        span = raw.span(venue, symbol, interval_s)
        if span is None:
            continue
        computed = compute_holdout_window(list(span), fraction=fraction)
        assert computed is not None  # span is non-None => two timestamps
        key = f"{venue.value}|{symbol}|{interval_s}"
        window = floored_window(computed, key, venue, interval_s, prior)
        boundary_month = month_of(window.start.astimezone(UTC))

        # rebuild both sides for this series (never leave a stale partition either side)
        for side in (research, holdout):
            side_dir = side.series_dir(venue, symbol, interval_s)
            if side_dir.exists():
                shutil.rmtree(side_dir)
            side_dir.mkdir(parents=True, exist_ok=True)

        raw_dir = raw.series_dir(venue, symbol, interval_s)
        for month_file in sorted(raw_dir.glob("*.parquet")):
            month = month_file.stem
            if month < boundary_month:
                shutil.copyfile(
                    month_file, research.series_dir(venue, symbol, interval_s) / month_file.name
                )
            elif month > boundary_month:
                shutil.copyfile(
                    month_file, holdout.series_dir(venue, symbol, interval_s) / month_file.name
                )
            else:  # the boundary month: one row-level split at window.start
                table = pq.read_table(month_file)
                cut = pc.less(table.column("start"), window.start)
                research_part = table.filter(cut)
                holdout_part = table.filter(pc.invert(cut))
                if research_part.num_rows:
                    pq.write_table(
                        research_part,
                        research.series_dir(venue, symbol, interval_s) / month_file.name,
                    )
                if holdout_part.num_rows:
                    pq.write_table(
                        holdout_part,
                        holdout.series_dir(venue, symbol, interval_s) / month_file.name,
                    )
        windows[key] = window
    # Carry forward prior manifest entries for series NOT in this raw store: dropping them
    # would erase their monotonic floor (a later re-seal with backward-extended history could
    # then move a boundary backward — TEST-3-adjacent). The bar-store seal shares this
    # property; parity follow-up tracked in TASKS.md.
    manifest_path = holdout.root / "_windows.json"
    if manifest_path.exists():
        prior_manifest: dict[str, dict[str, str]] = json.loads(manifest_path.read_text())
        for key, entry in prior_manifest.items():
            if key not in {f"{v.value}|{s}|{i}" for v, s, i in _series_coords(raw)}:
                windows.setdefault(
                    key,
                    HoldoutWindow(
                        start=datetime.fromisoformat(entry["start"]),
                        end=datetime.fromisoformat(entry["end"]),
                        version=entry["version"],
                    ),
                )
    write_seal_manifest(holdout.root, windows)
    return windows


def print_seal_report(windows: dict[str, HoldoutWindow]) -> None:
    for key, w in sorted(windows.items()):
        print(f"  {key}: holdout from {w.start.astimezone(UTC):%Y-%m-%d %H:%M:%S} (v{w.version})")
