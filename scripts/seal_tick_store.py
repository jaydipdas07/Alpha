"""Seal the raw tick store into research + holdout tick stores (TEST-3 at 1s scale).

The tick-scale sibling of ``scripts/seal_cold_store.py`` — same fraction
(``rigor.yaml holdout.fraction``), same monotonic floor (the holdout root's
``_windows.json``), month-file splits instead of series rewrites. Run **after**
``scripts/ingest_binance_ticks.py`` and **before** any lead-lag/liquidation sweep.

    uv run python scripts/seal_tick_store.py
"""

from __future__ import annotations

import os
from pathlib import Path

from alpha_core.data.tick_seal import print_seal_report, seal_tick_store
from alpha_core.data.tick_store import TickStore
from alpha_core.helpers.config import load_rigor_config

_ROOT = Path(__file__).resolve().parents[1]


def _root(env: str, default: str) -> Path:
    return Path(os.environ.get(env) or (_ROOT / default))


def main() -> None:
    raw = TickStore(_root("ALPHA_TICK_ROOT", "data_cold_ticks"))
    research = TickStore(_root("ALPHA_TICK_RESEARCH_ROOT", "data_research_ticks"))
    holdout = TickStore(_root("ALPHA_TICK_HOLDOUT_ROOT", "data_holdout_ticks"))
    fraction = load_rigor_config().holdout.fraction
    print(f"=== seal ticks {raw.root} -> research {research.root} + holdout {holdout.root} ===")
    windows = seal_tick_store(raw, research, holdout, fraction=fraction)
    print_seal_report(windows)
    print(f"=== sealed {len(windows)} tick series ===")


if __name__ == "__main__":
    main()
