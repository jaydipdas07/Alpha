"""The one-shot holdout gate (M3.7, Workflow B) — the risk-officer's final confirmation.

Before a discovered strategy reaches the human FORM it must clear one last hurdle: run
the candidate **once** on the *locked* holdout and confirm the edge holds with full rigor
on data the discovery loop never saw. This is the **single legitimate read of the holdout**
(TEST-3 / R6): the holdout lives in a physically-separate, no-ACL gate-only store, and
``HoldoutBarsFor`` is the only path that reads it — it is **never** handed to the
discovery loop, a pod agent, or any cockpit view.

``HoldoutGate`` takes an injected ``Backtester`` (the production one is ``EngineBacktester``
wired to a ``HoldoutBarsFor``) + the ``QuantAnalyst``: it runs the proposal on the holdout,
applies the same promote/reject/revise rigor verdict, and **passes only on PROMOTE**. The
trial count is the cell's cumulative count (the holdout is one more test of the same cell,
so the DSR deflation still applies). Research-plane: it reads the Parquet holdout store.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from alpha_core.core.enums import AssetClass
from alpha_core.core.models import Bar
from alpha_core.data.holdout import HoldoutStore
from alpha_core.helpers.config import load_discovery_config
from alpha_core.research.cold_store_bars import CellKey, SeriesCoord
from alpha_core.research.discovery import Backtester
from alpha_core.research.quant_analyst import QuantAnalyst, Verdict
from alpha_core.research.strategist import StrategyProposal


class HoldoutBarsFor:
    """A ``BarsFor`` backed by the gate-only :class:`HoldoutStore` — the ONE legitimate
    holdout read (TEST-3). Resolves a cell ``(market, window)`` to its configured series and
    reads the **holdout** bars. Structurally isolated: never given to the research/agent
    surface, which is handed ``ColdStoreBarsFor`` (the holdout-free cold store) instead."""

    def __init__(self, holdout: HoldoutStore, cells: Mapping[CellKey, SeriesCoord]) -> None:
        self._holdout = holdout
        self._cells: dict[CellKey, SeriesCoord] = dict(cells)

    @classmethod
    def from_config(cls, holdout: HoldoutStore) -> HoldoutBarsFor:
        """Build the cell map from ``config/discovery.yaml`` over the gate-only holdout store."""
        cfg = load_discovery_config()
        cells = {
            (cell.market, cell.window): SeriesCoord(cell.symbol, cell.venue, cell.interval_seconds)
            for cell in cfg.cells
        }
        return cls(holdout, cells)

    def __call__(self, market: AssetClass, window: str) -> list[Bar]:
        coord = self._cells.get((market, window))
        if coord is None:
            known = sorted(f"{m.value}/{w}" for m, w in self._cells)
            raise ValueError(
                f"no discovery cell mapped for {market.value}/{window!r}; known cells: {known}."
            )
        bars = self._holdout.read_holdout(
            symbol=coord.symbol, venue=coord.venue, interval_seconds=coord.interval_seconds
        )
        if bars and bars[0].asset_class != market:
            raise ValueError(
                f"holdout cell {market.value}/{window!r} maps to {coord.symbol}, whose bars "
                f"are {bars[0].asset_class.value}, not {market.value} — fix discovery.yaml."
            )
        return bars


class HoldoutPanelBarsFor:
    """A panel ``PanelBarsFor`` backed by the gate-only :class:`HoldoutStore` — the ONE legitimate
    holdout read for a *panel* (TEST-3). The panel analogue of :class:`HoldoutBarsFor`: resolves a
    panel ``(market, name)`` to its member series and reads each member's **holdout** bars.
    Structurally isolated — never handed to the research/agent surface (which gets the holdout-free
    cold store). Un-ingested members read back empty and are dropped (the panel backtester's
    ``>= 2*n_groups`` guard surfaces a panel that is too thin overall)."""

    def __init__(self, holdout: HoldoutStore, panels: Mapping[CellKey, list[SeriesCoord]]) -> None:
        self._holdout = holdout
        self._panels: dict[CellKey, list[SeriesCoord]] = dict(panels)

    @classmethod
    def from_config(cls, holdout: HoldoutStore) -> HoldoutPanelBarsFor:
        """Build the panel -> member-series map from ``config/discovery.yaml`` over the holdout."""
        cfg = load_discovery_config()
        panels = {
            (panel.market, panel.name): [
                SeriesCoord(symbol, panel.venue, panel.interval_seconds) for symbol in panel.symbols
            ]
            for panel in cfg.panels
        }
        return cls(holdout, panels)

    def __call__(self, market: AssetClass, window: str) -> dict[str, list[Bar]]:
        coords = self._panels.get((market, window))
        if coords is None:
            known = sorted(f"{m.value}/{w}" for m, w in self._panels)
            raise ValueError(
                f"no discovery panel mapped for {market.value}/{window!r}; known panels: {known}."
            )
        members: dict[str, list[Bar]] = {}
        for coord in coords:
            bars = self._holdout.read_holdout(
                symbol=coord.symbol, venue=coord.venue, interval_seconds=coord.interval_seconds
            )
            if not bars:
                continue
            if bars[0].asset_class != market:
                raise ValueError(
                    f"holdout panel {market.value}/{window!r} member {coord.symbol} is "
                    f"{bars[0].asset_class.value}, not {market.value} — fix discovery.yaml."
                )
            members[coord.symbol] = bars
        return members


@dataclass(frozen=True, slots=True)
class HoldoutGateResult:
    """The one-shot holdout gate's verdict (feeds the human FORM; never an agent view)."""

    passed: bool
    verdict: Verdict
    oos_sharpe: float
    n_obs: int
    reason: str


class HoldoutGate:
    """Run a candidate once on the locked holdout and confirm the edge with the rigor verdict."""

    def __init__(
        self,
        *,
        backtester: Backtester,
        quant_analyst: QuantAnalyst,
        oos_fraction: float | None = None,
    ) -> None:
        # ``backtester`` MUST be an EngineBacktester wired to a HoldoutBarsFor (the holdout read).
        self._backtester = backtester
        self._qa = quant_analyst
        self._oos_fraction = oos_fraction

    def evaluate(
        self, proposal: StrategyProposal, *, n_trials: int, trial_sharpe_variance: float
    ) -> HoldoutGateResult:
        """The one-shot read: run ``proposal`` on the holdout, then the rigor verdict.

        ``passed`` iff the verdict is **PROMOTE** — a real, significant, consistent edge on
        the never-seen holdout. ``n_trials`` is the cell's cumulative trial count (the holdout
        is one further test of the cell, so the DSR deflation still applies)."""
        returns = self._backtester.run(proposal)  # the single legitimate holdout read (TEST-3)
        # oos_fraction=None -> the quant-analyst's CALIBRATED operating point (rigor.yaml). The
        # DSR threshold (0.92) was calibrated at that oos_fraction, so judging the holdout at the
        # same slice keeps the threshold valid (a different slice would invalidate the calibration);
        # it is also conservative — it can only make PROMOTE harder, never falsely promote.
        assessment = self._qa.assess(
            returns,
            n_trials=n_trials,
            trial_sharpe_variance=trial_sharpe_variance,
            oos_fraction=self._oos_fraction,
        )
        return HoldoutGateResult(
            passed=assessment.verdict is Verdict.PROMOTE,
            verdict=assessment.verdict,
            oos_sharpe=assessment.oos_sharpe,
            n_obs=len(returns),
            reason=assessment.reason,
        )
