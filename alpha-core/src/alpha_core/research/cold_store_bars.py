"""The cold-store ``bars_for`` adapter (B1b.3d) — the production source the discovery loop's real
backtester reads its in-sample bars from.

``EngineBacktester`` (B1b.3c) takes an injected ``BarsFor`` — ``(market, window) -> list[Bar]`` —
and is a pure pass-through of whatever it returns; the holdout-isolation guarantee (TEST-3/R6) is
delegated to *this* boundary. ``ColdStoreBarsFor`` is that production boundary: it resolves a
discovery cell ``(market, window)`` to its configured cold-store series and reads it back.

**Why this is in-sample-only — and why the adapter adds no holdout filter.** The cold store is the
no-ACL *research* store. ``data.holdout.seal_dataset`` partitions the canonical dataset before the
research loop ever runs: research bars are written to the cold store, the rolled-forward holdout
tail is written to a *physically separate* :class:`~alpha_core.data.holdout.HoldoutStore` at a
disjoint root. So the cold store **does not contain the holdout** — isolation is structural, a
property of where the bytes live, not a runtime check that could fail open. This adapter therefore
reads the whole mapped series and passes it through: it deliberately does **not** re-apply a holdout
time-filter, because a second guard here would be a drift-prone duplicate of the one boundary that
actually enforces TEST-3 (and would mask a sealing bug rather than surface it). Its in-sample
guarantee is *inherited* from the cold store being sealed; ``test_cold_store_bars`` proves that
after a real seal the adapter yields zero holdout-window bars.

Construct it with an explicit cell map (pure + trivially testable) or via :meth:`from_config`, which
loads the map from ``config/discovery.yaml``. The cold-store root is the caller's concern (the
nightly-discovery wiring opens the :class:`~alpha_core.data.store.BarStore`) — alpha-core holds no
deployment path. Research-plane only: it imports the Parquet/DuckDB store (dev-group deps), so the
lean live worker never pulls it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.store import BarStore
from alpha_core.helpers.config import load_discovery_config


@dataclass(frozen=True, slots=True)
class SeriesCoord:
    """The cold-store coordinates of a cell's series — what ``BarStore.read_bars`` is keyed by."""

    symbol: str
    venue: Venue
    interval_seconds: int


# A discovery cell as the ``bars_for`` seam sees it: (market, window). The family is not part of the
# data coordinate — every strategy family in a cell is backtested on the same series.
CellKey = tuple[AssetClass, str]


class ColdStoreBarsFor:
    """A ``BarsFor`` (see ``engine_backtester.BarsFor``) backed by the no-ACL cold store.

    Resolves ``(market, window)`` to its configured series and reads it back as in-sample bars. A
    pure pass-through — no holdout filter (isolation is structural at the cold store; see the module
    docstring). Callable, so it drops straight into ``EngineBacktester(bars_for=...)``.
    """

    def __init__(self, store: BarStore, cells: Mapping[CellKey, SeriesCoord]) -> None:
        self._store = store
        self._cells: dict[CellKey, SeriesCoord] = dict(cells)

    @classmethod
    def from_config(cls, store: BarStore) -> ColdStoreBarsFor:
        """Build the cell map from ``config/discovery.yaml`` over ``store`` (honors
        ``ALPHA_CONFIG_DIR``). The config's duplicate-cell check has already run at load."""
        cfg = load_discovery_config()
        cells = {
            (cell.market, cell.window): SeriesCoord(cell.symbol, cell.venue, cell.interval_seconds)
            for cell in cfg.cells
        }
        return cls(store, cells)

    @property
    def cells(self) -> dict[CellKey, SeriesCoord]:
        """The resolved cell -> series map (a copy; for inspection / the nightly wiring)."""
        return dict(self._cells)

    def __call__(self, market: AssetClass, window: str) -> list[Bar]:
        """Return the cell's in-sample bars from the cold store (sorted by start). Raises
        ``ValueError`` for a cell with no configured series — a config gap fails fast and
        cell-identifying, never a silent empty result that the rigor gate would misread."""
        coord = self._cells.get((market, window))
        if coord is None:
            known = sorted(f"{m.value}/{w}" for m, w in self._cells)
            raise ValueError(
                f"no discovery cell mapped for {market.value}/{window!r}; known cells: {known}. "
                "Add it to config/discovery.yaml."
            )
        return self._store.read_bars(
            symbol=coord.symbol,
            venue=coord.venue,
            interval_seconds=coord.interval_seconds,
        )
