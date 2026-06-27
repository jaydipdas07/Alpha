"""Seal the raw cold store into a research store + a holdout store (B1a.6 / holdout wiring).

The ingest (``scripts/ingest_cold_store.py``) writes a **raw** store with every bar. Discovery must
not see the recent tail (the holdout) — so this seals each series' own rolled-forward holdout:
research bars go to the **research** store (what the nightly reads), the recent tail to
the **holdout** store (gate-only, a disjoint root). Run it **after ingest, before the nightly**.

Roots default to the repo-gitignored ``data_cold`` / ``data_research`` / ``data_holdout`` and are
overridable via ``ALPHA_COLD_ROOT`` / ``ALPHA_RESEARCH_ROOT`` / ``ALPHA_HOLDOUT_ROOT`` (the nightly
resolves ``ALPHA_RESEARCH_ROOT`` likewise). The holdout fraction is from ``config/rigor.yaml``.

Mac-CLI / no network. Run:

    uv run python scripts/seal_cold_store.py
"""

from __future__ import annotations

import os
from pathlib import Path

from alpha_core.core.enums import Venue
from alpha_core.data.holdout import seal_cold_store
from alpha_core.data.store import BarStore
from alpha_core.helpers.config import load_rigor_config

_ROOT = Path(__file__).resolve().parents[1]


def _root(env: str, default: str) -> Path:
    return Path(os.environ.get(env) or (_ROOT / default))


RAW_ROOT = _root("ALPHA_COLD_ROOT", "data_cold")  # raw ingestion store (source)
RESEARCH_ROOT = _root("ALPHA_RESEARCH_ROOT", "data_research")  # discovery reads this
HOLDOUT_ROOT = _root("ALPHA_HOLDOUT_ROOT", "data_holdout")  # gate-only


def main() -> None:
    fraction = load_rigor_config().holdout.fraction
    print(f"=== seal {RAW_ROOT} -> research {RESEARCH_ROOT} + holdout {HOLDOUT_ROOT} ===")
    research = BarStore(RESEARCH_ROOT)
    windows = seal_cold_store(
        BarStore(RAW_ROOT), research=research, holdout=BarStore(HOLDOUT_ROOT), fraction=fraction
    )
    for key, window in sorted(windows.items()):
        venue, symbol, interval = key.split("|")
        n_research = len(
            research.read_bars(symbol=symbol, venue=Venue(venue), interval_seconds=int(interval))
        )
        print(f"  {key}: {n_research} research bars; holdout from {window.start.date()}")
    print(f"=== sealed {len(windows)} series — discovery reads {RESEARCH_ROOT} ===")


if __name__ == "__main__":
    main()
