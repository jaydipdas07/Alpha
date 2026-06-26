"""The nightly discovery run (B1b.4a) — sweep the configured universe and collect survivors.

The *portable* discovery run the Lemma ``nightly-discovery`` schedule (B1b.4b) fires on the
research box. It composes the Phase-1b pieces into one batch: for each cell in the discovery
universe (``config/discovery.yaml``) it runs a
:func:`~alpha_core.research.discovery.run_discovery_cycle` per configured strategy template — the
strategist proposes (holdout-isolated, TEST-3), the real engine backtests the cell's cold-store
in-sample bars (``ColdStoreBarsFor`` -> ``EngineBacktester``), the quant-analyst adjudicates, and
the promoted candidates are the night's survivors.

Two design points carried from earlier increments:

* **Per-cell capital scale.** A crypto cell ($) and an equity cell (INR) cannot share one
  ``base_capital`` (the risk limits derive from it), so each cell's backtester gets that cell's
  ``starting_cash`` *and* a risk config whose ``base_capital`` is aligned to it — limits scale
  with the cell, so neither market's orders are spuriously blocked or waved through.
* **Poison-candidate quarantine (R8, the B1a.8b discipline).** A single ``(cell, template)``
  cycle that *raises* — a too-thin cell, a template that can't run at the cell's frequency — is
  recorded and skipped, never allowed to stall the whole night. A *config* bug (an unknown
  template name) instead fails fast up front, before any backtest runs.

Research-plane only (imports the cold store / engine); the lean live worker never imports it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from alpha_core.core.enums import AssetClass
from alpha_core.data.store import BarStore
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.helpers.config import (
    DiscoveryCellConfig,
    load_discovery_config,
    load_yaml,
)
from alpha_core.research.cold_store_bars import ColdStoreBarsFor
from alpha_core.research.discovery import Backtester, DiscoveryReport, run_discovery_cycle
from alpha_core.research.engine_backtester import EngineBacktester
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.quant_analyst import QuantAnalyst
from alpha_core.research.strategist import TEMPLATES, Proposer, Strategist, StrategyProposal
from alpha_core.risk.limits import RiskConfig, load_risk_config

# Every registered strategy family, sorted for a deterministic run order. A cell with no explicit
# ``templates`` searches all of them (the rigor gate culls the families that don't fit the cell).
ALL_TEMPLATES: tuple[str, ...] = tuple(sorted(TEMPLATES))

# A per-cell backtester source: production builds an EngineBacktester over the cold store; tests
# inject a fake. Injected (like ``run_discovery_cycle``'s backtester) so the loop stays pure.
BacktesterFor = Callable[[DiscoveryCellConfig], Backtester]


@dataclass(frozen=True, slots=True)
class QuarantinedCell:
    """A ``(cell, template)`` whose discovery cycle raised — recorded, not fatal: the night goes on.
    ``error`` is the exception ``repr`` so the failure is visible without re-running."""

    market: AssetClass
    window: str
    template: str
    error: str


@dataclass(frozen=True, slots=True)
class NightlyReport:
    """One nightly run's outcome: every (cell, template) cycle that completed, every one that was
    quarantined, and (derived) the promoted survivors across them all."""

    reports: list[DiscoveryReport]
    quarantined: list[QuarantinedCell]

    @property
    def survivors(self) -> list[StrategyProposal]:
        """Every promoted candidate across all cells/templates."""
        return [survivor for report in self.reports for survivor in report.survivors]


def _templates_for(cell: DiscoveryCellConfig) -> Sequence[str]:
    """The templates to search in a cell — its explicit list, or all registered families."""
    return cell.templates or ALL_TEMPLATES


def _validate_templates(cells: Sequence[DiscoveryCellConfig]) -> None:
    """Fail fast on an unknown template name in the universe — a config bug, before any backtest."""
    unknown = sorted({t for cell in cells for t in _templates_for(cell) if t not in TEMPLATES})
    if unknown:
        raise ValueError(
            f"unknown template(s) in the discovery universe: {unknown}; "
            f"known: {sorted(TEMPLATES)}. Fix config/discovery.yaml."
        )


def run_nightly_discovery(
    cells: Sequence[DiscoveryCellConfig],
    *,
    strategist: Strategist,
    quant_analyst: QuantAnalyst,
    backtester_for: BacktesterFor,
    n_candidates: int,
) -> NightlyReport:
    """Run a discovery cycle for every ``(cell, template)`` over the universe and collect survivors.

    The per-cell backtester is built by the injected ``backtester_for`` (production: a cold-store
    EngineBacktester; tests: a fake). A cycle that **raises** is quarantined and skipped — one bad
    combo never stalls the night — while an unknown template name fails fast up front (a config bug,
    not a runtime condition)."""
    _validate_templates(cells)
    reports: list[DiscoveryReport] = []
    quarantined: list[QuarantinedCell] = []
    for cell in cells:
        backtester = backtester_for(cell)
        for template_name in _templates_for(cell):
            try:
                reports.append(
                    run_discovery_cycle(
                        template_name,
                        market=cell.market,
                        window=cell.window,
                        strategist=strategist,
                        quant_analyst=quant_analyst,
                        backtester=backtester,
                        n_candidates=n_candidates,
                    )
                )
            except Exception as exc:  # quarantine ANY failure — never stall the night (R8)
                quarantined.append(
                    QuarantinedCell(cell.market, cell.window, template_name, repr(exc))
                )
    return NightlyReport(reports, quarantined)


def engine_backtester_for(
    bars_for: ColdStoreBarsFor,
    *,
    risk_config: RiskConfig,
    cost_config: dict[str, object],
    stress: bool = False,
) -> BacktesterFor:
    """Build a per-cell :class:`EngineBacktester` factory over ``bars_for``. Each cell's backtester
    gets instruments from the market (``asset_class`` only — equity/crypto slippage is bps,
    no tick needed), the cell venue and ``starting_cash``, and a risk config whose ``base_capital``
    is **aligned to that ``starting_cash``** so the limits scale with the cell (a $-crypto cell and
    an INR-equity cell never share one base)."""

    def _make(cell: DiscoveryCellConfig) -> Backtester:
        return EngineBacktester(
            bars_for=bars_for,
            instruments={cell.symbol: InstrumentMeta(asset_class=cell.market)},
            risk_config=risk_config.model_copy(update={"base_capital": cell.starting_cash}),
            cost_config=cost_config,
            venue=cell.venue,
            starting_cash=cell.starting_cash,
            stress=stress,
        )

    return _make


def run_nightly_discovery_from_config(
    store: BarStore,
    *,
    ledger_path: str | Path = ":memory:",
    proposer: Proposer | None = None,
    stress: bool = False,
) -> NightlyReport:
    """Production entry point: load the discovery universe + risk + cost config, open the durable
    proposal ledger, and run the nightly discovery over ``store`` (the cold store).

    The proposer defaults to a fresh (unseeded) ``RandomProposer`` so each night explores new
    configs — the ``ProposalLedger`` dedups any re-proposal idempotently across runs, so a repeat
    is a no-op, never double-counting the DSR trial penalty. Pass a seeded proposer to reproduce."""
    cfg = load_discovery_config()
    backtester_for = engine_backtester_for(
        ColdStoreBarsFor.from_config(store),
        risk_config=load_risk_config(),
        cost_config=load_yaml("costs.yaml"),
        stress=stress,
    )
    with ProposalLedger(ledger_path) as ledger:
        return run_nightly_discovery(
            cfg.cells,
            strategist=Strategist(ledger, proposer=proposer),
            quant_analyst=QuantAnalyst(),
            backtester_for=backtester_for,
            n_candidates=cfg.n_candidates,
        )
