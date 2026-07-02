"""Seal the raw options store into research + holdout stores (M5.5b — the options seal).

The ingest (``scripts/ingest_nse_fo.py``) writes a **raw** store with every chain day. Discovery
must not see the recent tail — this reserves each ``(venue, underlying)`` series' own
rolled-forward holdout by ``trade_date``, under the same monotonic-floor discipline as the bar
seal (``data.options_store.seal_options_store``). A separate seal domain from the bar stores:
its own roots, its own ``_windows.json``, and — on the FIRST seal — a genuinely never-read
holdout. Run **after ingest, before any options sweep**.

Roots default to the repo-gitignored ``data_options`` / ``options_research`` / ``options_holdout``
and are overridable via ``ALPHA_OPTIONS_ROOT`` / ``ALPHA_OPTIONS_RESEARCH_ROOT`` /
``ALPHA_OPTIONS_HOLDOUT_ROOT``. The holdout fraction is from ``config/rigor.yaml``.

Mac-CLI / no network. Run:

    uv run python scripts/seal_options_store.py
"""

from __future__ import annotations

import os
from pathlib import Path

from alpha_core.core.enums import Venue
from alpha_core.data.options_store import OptionsStore, seal_options_store
from alpha_core.helpers.config import load_rigor_config

_ROOT = Path(__file__).resolve().parents[1]


def _root(env: str, default: str) -> Path:
    return Path(os.environ.get(env) or (_ROOT / default))


RAW_ROOT = _root("ALPHA_OPTIONS_ROOT", "data_options")  # raw ingestion store (source)
RESEARCH_ROOT = _root("ALPHA_OPTIONS_RESEARCH_ROOT", "options_research")  # discovery reads this
HOLDOUT_ROOT = _root("ALPHA_OPTIONS_HOLDOUT_ROOT", "options_holdout")  # gate-only


def main() -> None:
    fraction = load_rigor_config().holdout.fraction
    print(f"=== seal {RAW_ROOT} -> research {RESEARCH_ROOT} + holdout {HOLDOUT_ROOT} ===")
    research = OptionsStore(RESEARCH_ROOT)
    windows = seal_options_store(
        OptionsStore(RAW_ROOT),
        research=research,
        holdout=OptionsStore(HOLDOUT_ROOT),
        fraction=fraction,
    )
    for key, window in sorted(windows.items()):
        venue_s, underlying, _interval = key.split("|")
        n_research = len(research.read(underlying=underlying, venue=Venue(venue_s)))
        print(f"  {key}: {n_research} research quotes; holdout from {window.start.date()}")
    print(f"=== sealed {len(windows)} series — options discovery reads {RESEARCH_ROOT} ===")


if __name__ == "__main__":
    main()
