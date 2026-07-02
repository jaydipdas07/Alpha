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
from datetime import datetime
from pathlib import Path

from alpha_core.core.enums import Venue
from alpha_core.data.holdout import prior_window_starts, seal_cold_store
from alpha_core.data.store import BarStore
from alpha_core.helpers.config import load_rigor_config

_ROOT = Path(__file__).resolve().parents[1]


def _root(env: str, default: str) -> Path:
    return Path(os.environ.get(env) or (_ROOT / default))


RAW_ROOT = _root("ALPHA_COLD_ROOT", "data_cold")  # raw ingestion store (source)
RESEARCH_ROOT = _root("ALPHA_RESEARCH_ROOT", "data_research")  # discovery reads this
HOLDOUT_ROOT = _root("ALPHA_HOLDOUT_ROOT", "data_holdout")  # gate-only

# Twin-venue floor seeds (data/holdout.py seal_cold_store(seed_floors=...)): a NEW venue whose
# series twin an existing venue's inherits that venue's published holdout boundary at its
# introduction, so the two legs' holdouts align from birth. The basis SPOT leg twins the perp
# dailies (same symbols, same days) — without this, first-seal spot boundaries would compute
# fraction-of-span starts far earlier than the perps' already-researched boundary.
_FLOOR_TWINS: dict[tuple[Venue, int], tuple[Venue, int]] = {
    (Venue.BINANCE_SPOT, 86400): (Venue.BINANCE, 86400),
}


def _seed_floors(holdout_root: Path) -> dict[tuple[Venue, int], datetime]:
    """The twin-venue seeds derivable from the existing manifest: for each ``_FLOOR_TWINS`` target,
    the latest prior start among the twin venue's same-interval series (absent = no seed — a fresh
    store has no boundary to inherit)."""
    prior = prior_window_starts(holdout_root)
    seeds: dict[tuple[Venue, int], datetime] = {}
    for target, (twin_venue, twin_interval) in _FLOOR_TWINS.items():
        prefix, suffix = f"{twin_venue.value}|", f"|{twin_interval}"
        twin = max(
            (s for k, s in prior.items() if k.startswith(prefix) and k.endswith(suffix)),
            default=None,
        )
        if twin is not None:
            seeds[target] = twin
    return seeds


def main() -> None:
    fraction = load_rigor_config().holdout.fraction
    print(f"=== seal {RAW_ROOT} -> research {RESEARCH_ROOT} + holdout {HOLDOUT_ROOT} ===")
    research = BarStore(RESEARCH_ROOT)
    seeds = _seed_floors(HOLDOUT_ROOT)
    for (venue, interval), start in sorted(seeds.items()):
        print(f"  floor-seed {venue.value}|*|{interval}: {start.date()} (twin-venue boundary)")
    windows = seal_cold_store(
        BarStore(RAW_ROOT),
        research=research,
        holdout=BarStore(HOLDOUT_ROOT),
        fraction=fraction,
        seed_floors=seeds,
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
