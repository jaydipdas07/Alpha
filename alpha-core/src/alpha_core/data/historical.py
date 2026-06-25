"""Historical data loading from CSV (ADR 0001).

Reads CSV rows and normalizes them into ``Tick``/``Bar`` models. Used by the
replay feed and the backtester. No interpolation — rows are taken as given and
validated by the core models.
"""

from __future__ import annotations

import csv
from pathlib import Path

from alpha_core.core.models import Bar, Tick
from alpha_core.data.normalize import bar_from_row, tick_from_row


def load_ticks_csv(path: str | Path) -> list[Tick]:
    """Load and normalize a CSV of ticks (header row required)."""
    with Path(path).open(newline="") as fh:
        return [tick_from_row(row) for row in csv.DictReader(fh)]


def load_bars_csv(path: str | Path) -> list[Bar]:
    """Load and normalize a CSV of bars (header row required)."""
    with Path(path).open(newline="") as fh:
        return [bar_from_row(row) for row in csv.DictReader(fh)]
