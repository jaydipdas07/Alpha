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
from collections.abc import Mapping, Sequence
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


def assert_disjoint_roots(research_root: Path, holdout_root: Path) -> None:
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
    assert_disjoint_roots(research.root, holdout.root)
    window = compute_holdout_window([b.start for b in bars], fraction=fraction)
    if window is None:
        return None
    research_bars, holdout_bars = split_research_holdout(bars, window)
    research.write_bars(research_bars)
    holdout.replace(holdout_bars, window)
    return window


def _clear_parquet(store: BarStore) -> None:
    """Remove every parquet file from a store's root — a *fresh* seal. The holdout tail rolls
    forward and is not monotonic, so a re-seal must not leave a stale bar that would contaminate
    the research store or the one-shot gate."""
    for parquet in store.root.glob("*.parquet"):
        parquet.unlink()


def write_seal_manifest(root: Path, windows: dict[str, HoldoutWindow]) -> None:
    """Record each series' locked holdout window (its ``holdout_window_version`` + extent) so the
    eventual one-shot gate knows exactly which tail is reserved per series."""
    manifest = {
        key: {"start": w.start.isoformat(), "end": w.end.isoformat(), "version": w.version}
        for key, w in windows.items()
    }
    (root / "_windows.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))


def prior_window_starts(holdout_root: Path) -> dict[str, datetime]:
    """The previous seal's per-series window starts (from the ``_windows.json`` manifest), keyed
    ``"venue|symbol|interval"`` — the **monotonic floor** a re-seal must respect. Empty if the
    store was never sealed. Public: the seal script also reads it to derive a twin-venue
    ``seed_floors`` entry (the basis spot leg inherits the perps' boundary at its introduction —
    see :func:`seal_cold_store`)."""
    path = holdout_root / "_windows.json"
    if not path.exists():
        return {}
    manifest: dict[str, dict[str, str]] = json.loads(path.read_text())
    return {key: datetime.fromisoformat(d["start"]) for key, d in manifest.items()}


def floored_window(
    window: HoldoutWindow,
    key: str,
    venue: Venue,
    interval_seconds: int,
    prior: Mapping[str, datetime],
) -> HoldoutWindow:
    """Pin a computed window's start to the **monotonic floor** (TEST-3): the holdout boundary may
    only ever move FORWARD in time. Extending a series' history backward stretches its span, which
    would drag the fraction-of-span start backward into data the discovery loop has already
    researched — silently converting researched bars into "holdout".

    - A previously-sealed series ratchets: ``start = max(computed, its own prior start)`` (the
      forward roll, R5, still releases the rolled-past tail).
    - A series never sealed before **adopts the latest prior start among same-venue,
      same-interval siblings outright** — even when its own fraction-of-span start would be
      *later*. Its pre-boundary history was never holdout anywhere (the siblings' bars there were
      research), but everything at/after the pinned boundary must stay unseen: a recently-listed
      member whose whole life sits inside the siblings' never-seen window is therefore ALL
      holdout, not quietly handed to the research store. The floor is venue-scoped so an
      unrelated market's boundary can never set (or leak into) this one; a brand-new venue seeds
      its own boundaries — if its series are near-twins of an existing venue's (the basis spot
      leg vs the perps), seed its floor explicitly at that venue's introduction.
    - No applicable prior (a fresh store / a new venue): the computed window stands."""
    own = prior.get(key)
    if own is not None:
        if own <= window.start:
            return window
        return HoldoutWindow(start=own, end=window.end, version=_version(own, window.end))
    prefix, suffix = f"{venue.value}|", f"|{interval_seconds}"
    inherited = max(
        (s for k, s in prior.items() if k.startswith(prefix) and k.endswith(suffix)),
        default=None,
    )
    if inherited is None or inherited == window.start:
        return window
    return HoldoutWindow(start=inherited, end=window.end, version=_version(inherited, window.end))


def seal_cold_store(
    source: BarStore,
    *,
    research: BarStore,
    holdout: BarStore,
    fraction: float,
    seed_floors: Mapping[tuple[Venue, int], datetime] | None = None,
) -> dict[str, HoldoutWindow]:
    """Seal a **multi-series** cold store: reserve each series' OWN rolled-forward holdout tail.

    For every series in ``source`` the research bars (everything before that series' window start)
    go to ``research`` — the cold store the discovery loop reads — and the recent holdout tail to
    ``holdout`` (gate-only, a disjoint root). Each series gets its **own** window: their spans
    differ (a few weeks of intraday vs years of daily), so one *global* window would dump an
    all-recent series entirely into the holdout and leave it with no research data. The per-series
    windows (keyed ``"venue|symbol|interval"``) are recorded in a ``_windows.json`` manifest at the
    holdout root and returned.

    The research store ends up **holdout-free per series** — the structural TEST-3 guarantee
    ``ColdStoreBarsFor`` relies on (it reads the whole series and trusts this boundary). ``source``,
    ``research`` and ``holdout`` must be pairwise-disjoint roots so no Parquet glob crosses them; a
    re-seal rebuilds both targets from scratch.

    **The boundary is pinned monotonic (TEST-3):** each series' window start is floored at the
    previous seal's start (read from the ``_windows.json`` manifest *before* the rebuild), so a
    re-seal over backward-extended history can never drag the boundary back into already-researched
    data — the holdout stays the same never-seen tail and only ever grows FORWARD as new data
    arrives (the clamped windows are written back to the manifest, so the floor compounds).

    ``seed_floors`` — explicit floors for ``(venue, interval)`` pairs with no sealed history of
    their own, for when a BRAND-NEW venue's series twin an existing venue's (the basis spot leg vs
    the perps: same symbols, same days). Without a seed a new venue computes fresh
    fraction-of-span boundaries, which on long history would sit far EARLIER than the twin's
    already-published research boundary — misaligning the two legs' holdouts from birth. A seed
    joins the sibling-inheritance scan for its ``(venue, interval)`` (a series' own prior still
    ratchets first); it is never written to the manifest itself — the floored real windows are,
    so the floor compounds from the first seeded seal."""
    assert_disjoint_roots(source.root, research.root)
    assert_disjoint_roots(source.root, holdout.root)
    assert_disjoint_roots(research.root, holdout.root)
    prior = prior_window_starts(holdout.root)  # BEFORE the rebuild — the monotonic floor
    for (seed_venue, seed_interval), start in (seed_floors or {}).items():
        # a synthetic sibling entry: participates in floored_window's venue-scoped max scan for
        # never-sealed series; the "<floor-seed>" pseudo-symbol can never collide with a real key.
        prior.setdefault(f"{seed_venue.value}|<floor-seed>|{seed_interval}", start)
    _clear_parquet(research)
    _clear_parquet(holdout)
    windows: dict[str, HoldoutWindow] = {}
    for symbol, venue, interval_seconds in source.series():
        bars = source.read_bars(symbol=symbol, venue=venue, interval_seconds=interval_seconds)
        window = compute_holdout_window([b.start for b in bars], fraction=fraction)
        if window is None:  # pragma: no cover - a listed series always has >=1 bar
            continue
        key = f"{venue.value}|{symbol}|{interval_seconds}"
        window = floored_window(window, key, venue, interval_seconds, prior)
        research_bars, holdout_bars = split_research_holdout(bars, window)
        research.write_bars(research_bars)
        holdout.write_bars(holdout_bars)
        windows[key] = window
    write_seal_manifest(holdout.root, windows)
    return windows
