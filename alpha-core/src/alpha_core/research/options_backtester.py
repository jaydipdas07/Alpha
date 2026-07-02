"""The EOD option-chain backtester (M5.5b) — the ``discovery.Backtester`` for premium structures.

The fold owns the position lifecycle over daily chains: enter a structure at EOD settles
(charging the full Indian ``index_option`` cost stack per leg via the engine's ``CostModel`` —
one source of cost truth since Phase 0), mark it daily at settles, and cash-settle at expiry at
**intrinsic value from the final-settlement level**. The per-bar return series feeds the
**unchanged** rigor gate.

⚠️ **The expiry-day settle trap, put to work:** on a contract's expiry day NSE publishes the
UNDERLYING's final-settlement level in that row's ``settle`` column (see
:mod:`~alpha_core.data.options_store`). The fold therefore NEVER marks an expiring row at
``settle``-as-premium — it reads that value as the settlement level ``S`` and marks the leg at
``max(±(S - K), 0)``, which is exactly how NSE cash-settles index options.

**Normalization:** each day's P&L (per unit of index) is divided by the episode's entry-time
parity forward, so returns are fractions of one-index-unit notional — objective, no extra
tunable; Sharpe is scale-invariant. Flat days are honest zeros. **Look-ahead-clean (TEST-1):**
entries/marks use only the current day's EOD chain; DTE and strikes come from that chain; the
timeline is trade-date-indexed. **Holdout isolation (TEST-3):** the quotes source is injected —
research reads the sealed options research store, the gate the gate-only holdout store, wired in
one tested place (``promote.build_options_backtesters``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import cast

from alpha_core.core.enums import AssetClass, OptionRight, Side
from alpha_core.data.options_store import OptionQuote, OptionsStore
from alpha_core.execution.costs import CostModel, InstrumentMeta
from alpha_core.helpers.config import (
    DiscoveryOptionsCellConfig,
    load_discovery_config,
    load_rigor_config,
    load_yaml,
)
from alpha_core.research.cold_store_bars import CellKey
from alpha_core.research.strategist import DecimalRange, StrategyProposal, StrategyTemplate
from alpha_core.strategy.examples.index_premium import (
    CondorLeg,
    IronCondorConfig,
    IronCondorEod,
    parity_forward,
)

# The premium-structure template registry. ONE family, a deliberately TINY pre-registered grid
# (2 DTE targets x 3 short distances x 2 wing widths = 12 configs): the structure is standard,
# the edge question is premium-vs-costs, not parameter mining — the DSR deflation stays light.
OPTIONS_TEMPLATES: dict[str, StrategyTemplate] = {
    "iron_condor_eod": StrategyTemplate(
        "iron_condor_eod",
        "iron_condor_eod",
        IronCondorConfig,
        IronCondorEod,
        {
            # {7, 28}: the weekly vs monthly entry (a two-point grid via step = span).
            "target_dte": DecimalRange(Decimal(7), Decimal(28), Decimal(21)),
            "short_distance_pct": DecimalRange(Decimal("0.02"), Decimal("0.06"), Decimal("0.02")),
            "wing_width_pct": DecimalRange(Decimal("0.01"), Decimal("0.02"), Decimal("0.01")),
        },
    ),
}

# (market, cell-name) -> the day's full chain. CONTRACT: in-sample-only on the research side;
# isolation is structural at the store roots (the sealed research store holds no holdout day).
OptionQuotesFor = Callable[[AssetClass, str], list[OptionQuote]]


def _options_cells_from_config() -> dict[CellKey, DiscoveryOptionsCellConfig]:
    """The ``(market, name) -> cell`` map from ``discovery.yaml options_cells``."""
    return {(c.market, c.name): c for c in load_discovery_config().options_cells}


class ResearchOptionsFor:
    """An ``OptionQuotesFor`` over an :class:`OptionsStore` — the production research boundary
    (reads the SEALED options research store; the holdout lives at a disjoint root)."""

    def __init__(
        self, store: OptionsStore, cells: Mapping[CellKey, DiscoveryOptionsCellConfig]
    ) -> None:
        self._store = store
        self._cells: dict[CellKey, DiscoveryOptionsCellConfig] = dict(cells)

    @classmethod
    def from_config(cls, store: OptionsStore) -> ResearchOptionsFor:
        """Build the cell map from ``config/discovery.yaml`` over ``store``."""
        return cls(store, _options_cells_from_config())

    def __call__(self, market: AssetClass, window: str) -> list[OptionQuote]:
        cell = self._cells.get((market, window))
        if cell is None:
            known = sorted(f"{m.value}/{w}" for m, w in self._cells)
            raise ValueError(
                f"no options cell mapped for {market.value}/{window!r}; known: {known}. "
                "Add it to config/discovery.yaml options_cells."
            )
        return self._store.read(underlying=cell.underlying, venue=cell.venue)


class HoldoutOptionsFor(ResearchOptionsFor):
    """The gate-only options boundary — the ONE legitimate options-holdout read (TEST-3).
    Structurally identical to :class:`ResearchOptionsFor` but constructed ONLY over the
    gate-only holdout store by ``promote.build_options_backtesters``; never handed to the
    research/agent surface. The distinct type pins the wiring test."""


def _intrinsic(leg: CondorLeg, settlement_level: Decimal) -> Decimal:
    """Cash-settlement value per unit at the final-settlement level ``S``."""
    if leg.right is OptionRight.CALL:
        return max(settlement_level - leg.strike, Decimal(0))
    return max(leg.strike - settlement_level, Decimal(0))


def _leg_key(leg: CondorLeg) -> tuple[datetime, Decimal, OptionRight]:
    return (leg.expiry, leg.strike, leg.right)


class OptionsChainBacktester:
    """A ``discovery.Backtester`` that runs a premium-structure proposal over one underlying's
    daily EOD chains.

    ``quotes_for`` is the injected store boundary (research vs gate-only holdout — the TEST-3
    wiring lives in ``promote.build_options_backtesters``); the cell's contract facts
    (lot/tick/liquidity floor) come from ``discovery.yaml options_cells``. ``cost_model``
    overrides the engine cost model (tests pin a zero-cost one); ``None`` builds it from
    ``costs.yaml`` — the same ``index_option`` stack the live engine prices with."""

    def __init__(
        self,
        *,
        quotes_for: OptionQuotesFor,
        cells: Mapping[CellKey, DiscoveryOptionsCellConfig] | None = None,
        min_bars: int | None = None,
        cost_model: CostModel | None = None,
    ) -> None:
        self._quotes_for = quotes_for
        self._cells = dict(cells) if cells is not None else _options_cells_from_config()
        # the rigor gate needs >= 2*n_groups return observations; fail fast on a too-short cell.
        self._min_bars = min_bars if min_bars is not None else 2 * load_rigor_config().cpcv.n_groups
        self._cost_model = (
            cost_model if cost_model is not None else CostModel(load_yaml("costs.yaml"))
        )

    def run(self, proposal: StrategyProposal) -> list[float]:
        """Fold the proposal's structure lifecycle over the cell's daily chains and return the
        per-day normalized return series. Raises for an unknown template/cell or too few days."""
        template = OPTIONS_TEMPLATES.get(proposal.template)
        if template is None:
            raise ValueError(
                f"unknown options template {proposal.template!r}; "
                f"known: {sorted(OPTIONS_TEMPLATES)}"
            )
        strategy = cast(IronCondorEod, template.build(proposal.params))
        cell = self._cells.get((proposal.market, proposal.window))
        if cell is None:
            known = sorted(f"{m.value}/{w}" for m, w in self._cells)
            raise ValueError(
                f"no options cell mapped for {proposal.market.value}/{proposal.window!r}; "
                f"known: {known}. Add it to config/discovery.yaml options_cells."
            )
        quotes = self._quotes_for(proposal.market, proposal.window)
        by_day: dict[datetime, list[OptionQuote]] = {}
        for q in quotes:
            by_day.setdefault(q.trade_date, []).append(q)
        days = sorted(by_day)
        if len(days) < self._min_bars:
            raise ValueError(
                f"too few chain days for options cell {proposal.market.value}/"
                f"{proposal.window}: {len(days)} days < {self._min_bars} (the rigor floor)"
            )
        meta = InstrumentMeta(asset_class=proposal.market, tick_size=cell.tick_size)
        lot = Decimal(cell.lot_size)

        returns: list[float] = []
        position: list[CondorLeg] | None = None
        prev_marks: dict[tuple[datetime, Decimal, OptionRight], Decimal] = {}
        episode_forward: Decimal | None = None
        for day in days:
            chain = by_day[day]
            index = {(q.expiry, q.strike, q.right): q for q in chain}
            pnl = Decimal(0)
            settled_today = False

            if position is not None:
                for leg in position:
                    key = _leg_key(leg)
                    row = index.get(key)
                    if day == leg.expiry:
                        # THE TRAP, used correctly: the expiring row's `settle` IS the
                        # underlying's final-settlement level -> cash-settle at intrinsic.
                        if row is not None:
                            mark = _intrinsic(leg, row.settle)
                        else:
                            # the expiring row itself is missing: settle at the sibling
                            # chain's parity forward; if even that is absent, the last mark
                            # stands (a flat final day — conservative, never a fabricated
                            # settlement level).
                            siblings = [q for q in chain if q.expiry == leg.expiry]
                            level = parity_forward(siblings)
                            mark = _intrinsic(leg, level) if level is not None else prev_marks[key]
                    else:
                        row_settle = row.settle if row is not None else None
                        mark = row_settle if row_settle is not None else prev_marks[key]
                    pnl += leg.quantity * (mark - prev_marks[key])
                    prev_marks[key] = mark
                if day == position[0].expiry:  # all four legs share the expiry
                    position = None
                    prev_marks = {}
                    settled_today = True  # re-entry earliest tomorrow (one structure at a time)

            if position is None and not settled_today:
                dte_by_expiry = {
                    expiry: (expiry - day).days for expiry in {q.expiry for q in chain}
                }
                expiry = strategy.pick_expiry(dte_by_expiry)
                if expiry is not None:
                    exp_chain = [q for q in chain if q.expiry == expiry]
                    forward = parity_forward(exp_chain)
                    if forward is not None:
                        legs = strategy.select_structure(chain, expiry, forward, cell.min_oi)
                        if legs is not None:
                            leg_rows: dict[tuple[datetime, Decimal, OptionRight], OptionQuote] = {}
                            for leg in legs:
                                row = index.get(_leg_key(leg))
                                if row is None:
                                    break  # a selected strike without a row: no entry today
                                leg_rows[_leg_key(leg)] = row
                            shorts_priced = len(leg_rows) == len(legs) and all(
                                leg_rows[_leg_key(leg)].settle > 0
                                for leg in legs
                                if leg.quantity < 0  # a short must collect a REAL premium
                            )
                            if shorts_priced:
                                position = legs
                                episode_forward = forward
                                for leg in legs:
                                    entry_row = leg_rows[_leg_key(leg)]
                                    prev_marks[_leg_key(leg)] = entry_row.settle
                                    side = Side.BUY if leg.quantity > 0 else Side.SELL
                                    breakdown = self._cost_model.estimate(
                                        side=side,
                                        quantity=lot,
                                        ltp=entry_row.settle,
                                        instrument=meta,
                                    )
                                    pnl -= breakdown.total / lot  # per unit of index

            returns.append(float(pnl / episode_forward) if episode_forward is not None else 0.0)
        return returns
