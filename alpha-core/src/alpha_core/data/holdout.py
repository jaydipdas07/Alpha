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
import json
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
        """True iff ``ts`` is in the locked tail (at or after ``start``) and so must never
        be researched — the single definition ``split_research_holdout`` uses. ``end``
        records the data extent at lock time (and feeds ``version``); a bar arriving later
        is still holdout (it is even more recent)."""
        return ts >= self.start


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
        (holdout if window.contains(bar.start) else research).append(bar)
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

    def _window_path(self) -> Path:
        return self.root / "_window.json"

    def replace(self, bars: Sequence[Bar], window: HoldoutWindow) -> int:
        """**Rebuild** the isolated store to hold *exactly* the current locked window and
        record its ``holdout_window_version`` (R5). The holdout is the recent tail, which
        rolls forward and is **not** monotonic — so prior windows' bars are cleared, never
        merged (a re-seal must not leave a stale bar that contaminates the one-shot gate).
        Expects the current window's full set of bars across every series. Returns count."""
        for parquet in self.root.glob("*.parquet"):
            parquet.unlink()
        self._window_path().write_text(
            json.dumps(
                {
                    "start": window.start.isoformat(),
                    "end": window.end.isoformat(),
                    "version": window.version,
                }
            )
        )
        return self._store.write_bars(bars)

    def current_window(self) -> HoldoutWindow | None:
        """The window locked at the last :meth:`replace` (its ``holdout_window_version``),
        or ``None`` if nothing has been sealed yet."""
        path = self._window_path()
        if not path.exists():
            return None
        d = json.loads(path.read_text())
        return HoldoutWindow(
            start=datetime.fromisoformat(str(d["start"])),
            end=datetime.fromisoformat(str(d["end"])),
            version=str(d["version"]),
        )

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
    """Partition ``bars`` by the roll-forward window: research bars → the cold store,
    holdout bars → the isolated store. The cold store ends up with **no** holdout bars
    (the structural TEST-3 guarantee). Returns the locked window (R5), or ``None`` for
    empty input. Raises if the two store roots are not disjoint.

    Pass the **full canonical dataset** each call (it is the partition's source of truth):
    research grows monotonically as the window rolls forward (the cold store merges
    correctly), while the holdout store is **rebuilt** to exactly the current window — so a
    bar that leaves the window is promoted into the cold store and removed from the holdout,
    never left stale in both."""
    _assert_disjoint(research.root, holdout.root)
    window = compute_holdout_window([b.start for b in bars], fraction=fraction)
    if window is None:
        return None
    research_bars, holdout_bars = split_research_holdout(bars, window)
    research.write_bars(research_bars)
    holdout.replace(holdout_bars, window)
    return window
