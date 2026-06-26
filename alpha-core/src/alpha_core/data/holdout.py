"""Roll-forward locked holdout in a no-ACL store (R5/R6, B1a.6) — proves TEST-3.

The most recent slice of history is LOCKED AWAY in a physically separate store the
research surface (strategist / RAG / table-read agents) is never handed, so it cannot be
read, tuned against, or leaked through any agent tool (**TEST-3**). It is used once, as
the final one-shot gate.

- **Roll-forward (R5):** the holdout is always the most recent ``fraction`` of the time
  span, so as new data arrives the window rolls forward and the research loop can never
  reach the present. ``holdout_window_version`` records exactly which window is locked
  (it changes whenever the window moves).
- **No-ACL isolation (R6):** isolation is *structural*, not a permission flag that could
  fail open — the holdout bars are **absent** from the cold (research) store and live only
  in a separate :class:`HoldoutStore` at a **disjoint root**, so the cold store's Parquet
  glob can never reach them. ``seal_dataset`` enforces the disjoint-root invariant.

Research-plane only (the holdout never reaches the worker); it builds on the cold-store
:class:`~alpha_core.data.store.BarStore` (Parquet/DuckDB, dev-group deps).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from alpha_core.core.enums import Venue
from alpha_core.core.models import Bar
from alpha_core.data.store import BarStore


@dataclass(frozen=True, slots=True)
class HoldoutWindow:
    """The locked holdout time window (R5)."""

    start: datetime  # inclusive — the first locked timestamp
    end: datetime  # inclusive — the last data timestamp
    version: str  # holdout_window_version: a deterministic id of this locked window

    def contains(self, ts: datetime) -> bool:
        """True iff ``ts`` falls in the locked window (and so must never be researched)."""
        return self.start <= ts <= self.end


def _version(start: datetime, end: datetime) -> str:
    """A short, deterministic id for the locked window — it changes when the window rolls."""
    return hashlib.sha256(f"{start.isoformat()}|{end.isoformat()}".encode()).hexdigest()[:16]


def compute_holdout_window(
    timestamps: Sequence[datetime], *, fraction: float
) -> HoldoutWindow | None:
    """The locked window = the most recent ``fraction`` of the ``[min, max]`` span, so it
    rolls forward as ``max(timestamps)`` advances (R5). ``None`` for an empty input."""
    if not 0.0 < fraction < 1.0:
        raise ValueError(f"holdout fraction must be in (0, 1); got {fraction}")
    ordered = sorted(timestamps)
    if not ordered:
        return None
    start = ordered[-1] - (ordered[-1] - ordered[0]) * fraction
    return HoldoutWindow(start=start, end=ordered[-1], version=_version(start, ordered[-1]))


def split_research_holdout(
    bars: Sequence[Bar], window: HoldoutWindow
) -> tuple[list[Bar], list[Bar]]:
    """Partition ``bars`` into ``(research, holdout)`` by the locked window: a bar at or
    after ``window.start`` is holdout; everything earlier is research (agent-visible)."""
    research: list[Bar] = []
    holdout: list[Bar] = []
    for bar in bars:
        (holdout if bar.start >= window.start else research).append(bar)
    return research, holdout


class HoldoutStore:
    """A physically separate, gate-only store of the locked holdout bars (R6).

    The research surface is **never** handed this object — that *is* the isolation (there
    is no ACL to misconfigure). Only the final one-shot gate (Workflow B) reads it.
    """

    def __init__(self, root: str | Path) -> None:
        self._store = BarStore(root)

    @property
    def root(self) -> Path:
        return self._store.root

    def seal(self, bars: Sequence[Bar]) -> int:
        """Lock ``bars`` into the isolated store (idempotent). Returns bars on disk."""
        return self._store.write_bars(bars)

    def read_holdout(self, *, symbol: str, venue: Venue, interval_seconds: int) -> list[Bar]:
        """Read the locked holdout — **the one-shot final gate path only**. Never call
        this from the research / agent surface (that would defeat TEST-3)."""
        return self._store.read_bars(symbol=symbol, venue=venue, interval_seconds=interval_seconds)


def _assert_disjoint(research_root: Path, holdout_root: Path) -> None:
    """Reject overlapping roots: if the holdout lived inside (or as) the cold store's root,
    the cold store's Parquet glob could read it — breaking TEST-3."""
    r = research_root.resolve()
    h = holdout_root.resolve()
    if r == h or r in h.parents or h in r.parents:
        raise ValueError(
            f"holdout root {h} must be disjoint from the research root {r}: a shared or "
            "nested root would let the cold store read the holdout (TEST-3)"
        )


def seal_dataset(
    bars: Sequence[Bar], *, research: BarStore, holdout: HoldoutStore, fraction: float
) -> HoldoutWindow | None:
    """Split ``bars`` by the roll-forward holdout window and persist the research bars to
    the cold store and the holdout bars to the isolated store. The cold store ends up with
    **no** holdout bars — the structural TEST-3 guarantee. Returns the locked window (R5),
    or ``None`` for empty input. Raises if the two store roots are not disjoint."""
    _assert_disjoint(research.root, holdout.root)
    window = compute_holdout_window([b.start for b in bars], fraction=fraction)
    if window is None:
        return None
    research_bars, holdout_bars = split_research_holdout(bars, window)
    research.write_bars(research_bars)
    holdout.seal(holdout_bars)
    return window
