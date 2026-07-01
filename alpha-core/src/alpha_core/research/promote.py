"""Promote a discovered survivor through Workflow B's two gates (M3.0).

The discovery loop finds survivors; this module carries one **real** survivor to the risk-officer
review that feeds the human FORM. It is the off-pod, research-plane composer behind the
``scripts/risk_officer_review.py --survivor`` path — the production counterpart to that script's
``--demo`` mode (which fabricates a labelled TEST strategy with synthetic returns).

Two gates, both computed where the data physically lives (TEST-3):

1. ``HoldoutGate.evaluate`` — the candidate clears the rigor verdict on the *locked* holdout (the
   single legitimate read of the no-ACL holdout store).
2. ``evaluate_paper_run`` — forward paper-trading kept enough of the backtest edge.

``review_survivor`` takes **injected backtesters** (``Backtester`` protocol) so it is pure and
unit-testable; ``build_survivor_backtesters`` wires the production pair (in-sample over the sealed
research store, holdout over the gate-only ``HoldoutStore``). ``reconstruct_deflation_inputs``
recovers the DSR penalty the cell faced — the cumulative ledger trial count + the cross-trial Sharpe
variance over the cell's *actually-tried* in-sample population — since a discovery run record stores
only the survivor's params, not the variance.

Research-plane only (imports the Parquet/DuckDB store + the engine); the lean live worker never
imports it. No pod / Lemma / network here — the off-pod bridge does the pod I/O around it.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from alpha_core.data.holdout import HoldoutStore
from alpha_core.data.store import BarStore
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.helpers.config import DiscoveryCellConfig
from alpha_core.research.approval import RiskOfficerReview, assemble_review
from alpha_core.research.cold_store_bars import ColdStoreBarsFor
from alpha_core.research.discovery import Backtester
from alpha_core.research.engine_backtester import EngineBacktester
from alpha_core.research.holdout_gate import HoldoutBarsFor, HoldoutGate, HoldoutPanelBarsFor
from alpha_core.research.nightly import engine_backtester_for
from alpha_core.research.panel_backtester import ColdStorePanelBarsFor, PanelBacktester
from alpha_core.research.paper_eval import evaluate_paper_run
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.quant_analyst import QuantAnalyst, deflation_inputs
from alpha_core.research.strategist import (
    TEMPLATES,
    IntRange,
    ParamSpace,
    ParamSpec,
    ParamValue,
    StrategyProposal,
    proposal_fingerprint,
)
from alpha_core.risk.limits import RiskConfig


def make_proposal(
    template: str, params: Mapping[str, ParamValue], cell: DiscoveryCellConfig, *, trial_index: int
) -> StrategyProposal:
    """A ``StrategyProposal`` for a survivor in its cell (the fingerprint is the originality key the
    ledger uses; ``trial_index`` carries the cell's cumulative count for the DSR deflation)."""
    return StrategyProposal(
        template=template,
        params=dict(params),
        market=cell.market,
        window=cell.window,
        trial_index=trial_index,
        fingerprint=proposal_fingerprint(template, params),
    )


def build_survivor_backtesters(
    *,
    cell: DiscoveryCellConfig,
    research_store: BarStore,
    holdout_store: HoldoutStore,
    risk_config: RiskConfig,
    cost_config: dict[str, object],
) -> tuple[Backtester, Backtester]:
    """Wire the production (in-sample, holdout) backtester pair for ``cell``.

    The in-sample backtester reads the **sealed research** store (holdout-free, TEST-3) via the same
    ``engine_backtester_for`` the nightly uses; the holdout backtester reads the **gate-only**
    ``HoldoutStore`` via ``HoldoutBarsFor`` — the single legitimate holdout read. Both align the
    risk config's ``base_capital`` to the cell's ``starting_cash`` so limits scale with the cell.
    """
    in_sample = engine_backtester_for(
        ColdStoreBarsFor.from_config(research_store),
        risk_config=risk_config,
        cost_config=cost_config,
    )(cell)
    holdout = EngineBacktester(
        bars_for=HoldoutBarsFor.from_config(holdout_store),
        instruments={cell.symbol: InstrumentMeta(asset_class=cell.market)},
        risk_config=risk_config.model_copy(update={"base_capital": cell.starting_cash}),
        cost_config=cost_config,
        venue=cell.venue,
        starting_cash=cell.starting_cash,
    )
    return in_sample, holdout


def build_panel_backtesters(
    *, research_store: BarStore, holdout_store: HoldoutStore
) -> tuple[Backtester, Backtester]:
    """Wire the production (in-sample, holdout) **panel** backtester pair — the cross-sectional
    analogue of :func:`build_survivor_backtesters`.

    The in-sample backtester reads the **sealed research** store (holdout-free, TEST-3) via
    ``ColdStorePanelBarsFor``; the holdout backtester reads the **gate-only** ``HoldoutStore`` via
    ``HoldoutPanelBarsFor`` — the single legitimate holdout read. Keeping this wiring in one tested
    place guards the TEST-3 boundary (which store feeds which backtester) against a future edit. A
    panel needs no per-cell risk/cost wiring — the cross-sectional backtester scores returns
    directly (signal-quality), with its turnover cost sourced from ``costs.yaml``.
    """
    in_sample = PanelBacktester(panel_bars_for=ColdStorePanelBarsFor.from_config(research_store))
    holdout = PanelBacktester(panel_bars_for=HoldoutPanelBarsFor.from_config(holdout_store))
    return in_sample, holdout


def review_survivor(
    *,
    proposal: StrategyProposal,
    in_sample_backtester: Backtester,
    holdout_backtester: Backtester,
    paper_returns: Sequence[float],
    n_trials: int,
    trial_sharpe_variance: float,
    strategy_name: str,
    venue: str,
    quant_analyst: QuantAnalyst | None = None,
    mode: str = "paper",
    origin: str = "discovery",
) -> RiskOfficerReview:
    """Run a survivor through both Workflow-B gates and return the combined risk-officer review.

    ``passed`` requires BOTH: the candidate promotes on the never-seen holdout AND forward paper
    kept enough of the backtest Sharpe. Backtesters are injected so this is pure + testable;
    production wires them with :func:`build_survivor_backtesters`. ``n_trials`` /
    ``trial_sharpe_variance`` are the cell's DSR deflation inputs (see
    :func:`reconstruct_deflation_inputs`)."""
    qa = quant_analyst or QuantAnalyst()
    backtest_returns = list(in_sample_backtester.run(proposal))
    holdout = HoldoutGate(backtester=holdout_backtester, quant_analyst=qa).evaluate(
        proposal, n_trials=n_trials, trial_sharpe_variance=trial_sharpe_variance
    )
    paper = evaluate_paper_run(paper_returns=paper_returns, backtest_returns=backtest_returns)
    return assemble_review(
        strategy_name=strategy_name,
        family=proposal.template,
        market=proposal.market,
        venue=venue,
        paper=paper,
        holdout=holdout,
        mode=mode,
        origin=origin,
    )


def _spec_values(spec: ParamSpec) -> list[ParamValue]:
    """Every value in a bounded param spec (the strategist samples these; we enumerate them)."""
    if isinstance(spec, IntRange):
        ints: list[ParamValue] = [spec.low + i for i in range(spec.high - spec.low + 1)]
        return ints
    steps = int((spec.high - spec.low) / spec.step)  # DecimalRange grid: low, low+step, ..., high
    decimals: list[ParamValue] = [spec.low + spec.step * i for i in range(steps + 1)]
    return decimals


def _param_grid(space: ParamSpace) -> Iterator[dict[str, ParamValue]]:
    """Every config in a template's bounded param space (the cartesian product of its specs)."""
    names = sorted(space)
    for combo in itertools.product(*(_spec_values(space[name]) for name in names)):
        yield dict(zip(names, combo, strict=True))


def reconstruct_deflation_inputs(
    *,
    template: str,
    cell: DiscoveryCellConfig,
    research_store: BarStore,
    ledger: ProposalLedger,
    risk_config: RiskConfig,
    cost_config: dict[str, object],
    quant_analyst: QuantAnalyst | None = None,
) -> tuple[int, float]:
    """Recover the cell's DSR deflation inputs ``(n_trials, trial_sharpe_variance)`` for the holdout
    gate, since a discovery run record stores only the survivor's params, not the variance.

    ``n_trials`` is the cell's **cumulative** trial count (``ProposalLedger.count`` — the count the
    discovery cycle deflated by; the holdout is one further test of the same cell). The variance is
    computed over the cell's **actually-tried** in-sample population: enumerate the template's param
    grid, keep the configs whose fingerprint is in the ledger (the tried set), and backtest only
    those in-sample — as ``deflation_inputs`` does in the discovery cycle. Cost is bounded by the
    number of *tried* configs (a handful), not the whole grid (only hashed to match fingerprints).
    The variance is over the cell's *cumulative* tried set (vs a single cycle's trials) — identical
    for a single-cycle cell (M3.0), conservative for one searched across multiple nightly runs.
    """
    qa = quant_analyst or QuantAnalyst()
    family = TEMPLATES[template].family
    n_trials = ledger.count(cell.market, family, cell.window)
    tried = ledger.seen(cell.market, family, cell.window)  # the fingerprints actually proposed
    in_sample = engine_backtester_for(
        ColdStoreBarsFor.from_config(research_store),
        risk_config=risk_config,
        cost_config=cost_config,
    )(cell)
    population: list[Sequence[float]] = []
    for params in _param_grid(TEMPLATES[template].param_space):
        if proposal_fingerprint(template, params) in tried:
            population.append(
                list(in_sample.run(make_proposal(template, params, cell, trial_index=n_trials)))
            )
            if len(population) == len(tried):
                break  # found every tried fingerprint — no need to hash the rest of the grid
    _, variance = deflation_inputs(population, oos_fraction=qa.oos_fraction)
    return n_trials, variance


def parse_params(raw: Mapping[str, str], template: str) -> dict[str, ParamValue]:
    """Coerce string param values (CLI / run-record JSON) to the template's spec types (int for
    ``IntRange``, ``Decimal`` for ``DecimalRange``), failing fast on an unknown template / param."""
    space = TEMPLATES[template].param_space  # raises KeyError-as-config-bug upstream if unknown
    out: dict[str, ParamValue] = {}
    for name, value in raw.items():
        spec = space.get(name)
        if spec is None:
            raise ValueError(
                f"unknown param {name!r} for template {template!r}; known: {sorted(space)}"
            )
        try:
            out[name] = int(value) if isinstance(spec, IntRange) else Decimal(str(value))
        except (ValueError, InvalidOperation) as exc:
            raise ValueError(
                f"param {name}={value!r} is not a valid {type(spec).__name__}"
            ) from exc
    return out


def pick_survivor(
    record: Mapping[str, Any], *, window: str, template: str | None = None
) -> tuple[str, dict[str, str]]:
    """Pick a survivor (family + params) from a discovery run record (``NightlyReport.summary``).

    A window has up to one report per family, so several families can survive it. ``template``
    disambiguates; without it a window with exactly one promoted family is taken, and an ambiguous
    one (multiple promoted families, or >1 config in a family) **raises** rather than silently
    choosing. Raises ``ValueError`` when nothing matches."""
    reports = [
        r for r in record.get("reports", []) if r.get("window") == window and r.get("promoted")
    ]
    if template is not None:
        reports = [r for r in reports if r.get("family") == template]
    if not reports:
        suffix = f" / template {template!r}" if template else ""
        raise ValueError(f"no survivor for window {window!r}{suffix}")
    if len(reports) > 1:
        families = sorted(str(r["family"]) for r in reports)
        raise ValueError(f"window {window!r} has survivors in families {families}; pass --template")
    promoted = reports[0]["promoted"]
    if len(promoted) > 1:
        raise ValueError(
            f"window {window!r} / {reports[0]['family']!r} has {len(promoted)} survivors; "
            "narrow the selection (pass explicit --template/--params)"
        )
    return str(reports[0]["family"]), dict(promoted[0]["params"])
