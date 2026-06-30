"""The funding-carry panel backtester (M3.0) — the ``discovery.Backtester`` for the carry leg.

Like ``PanelBacktester`` it runs a dollar-neutral cross-sectional book, but the signal is funding
(rank by trailing-mean funding) and the per-bar return adds the **carry** to the price P&L:

    return[i->i+1] = Σ w·(price return)  +  Σ -w·(funding on day i+1)  -  turnover cost

A short perp (w < 0) *receives* funding when it is positive, a long *pays* it — so the carry book
(short high-funding, long low-funding) harvests the funding spread. The carry is a cash flow, a
different return source than price direction. The per-bar series feeds the **unchanged** rigor gate.

**Look-ahead-clean (TEST-1):** weights at ``i`` use funding up to ``i``; the carry and the price
move are both over ``i -> i+1``. **Holdout isolation (TEST-3)** is delegated to the injected
sources: in-sample the bars come from the sealed research store, and the bars' timeline gates the
funding to the in-sample window (the *structural* funding seal lands with the holdout read).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from decimal import Decimal
from typing import cast

from alpha_core.core.enums import AssetClass
from alpha_core.data.funding import FundingRate, FundingStore, daily_funding
from alpha_core.helpers.config import load_discovery_config, load_rigor_config
from alpha_core.research.cold_store_bars import CellKey, SeriesCoord
from alpha_core.research.panel_backtester import PanelBarsFor, align_closes, turnover_cost_fraction
from alpha_core.research.strategist import IntRange, StrategyProposal, StrategyTemplate
from alpha_core.strategy.examples.cross_sectional import (
    CrossSectionalConfig,
    FundingCarry,
    PanelStrategy,
)

# The funding-carry template registry (its own backtester, so kept apart from PANEL_TEMPLATES). The
# carry signal is most informative at a short trailing window; ``top_k <= 8`` fits any real panel.
FUNDING_TEMPLATES: dict[str, StrategyTemplate] = {
    "cross_sectional_carry": StrategyTemplate(
        "cross_sectional_carry",
        "cross_sectional_carry",
        CrossSectionalConfig,
        FundingCarry,
        {"lookback": IntRange(3, 30), "top_k": IntRange(2, 8), "holding_period": IntRange(1, 10)},
    ),
}

# (market, panel_name) -> {symbol: funding history}. CONTRACT: in-sample-only, like PanelBarsFor.
PanelFundingFor = Callable[[AssetClass, str], dict[str, list[FundingRate]]]


def _simulate_carry(
    strategy: PanelStrategy,
    closes: Mapping[str, list[Decimal]],
    funding: Mapping[str, list[Decimal]],
    n: int,
    cost: Decimal,
) -> list[float]:
    """The carry fold: re-rank by trailing funding every ``holding_period`` bars, hold the book
    between, and earn each bar's price move PLUS the carry (``Σ -w·funding``), net of turnover."""
    cfg = strategy.config
    warmup = cfg.lookback
    held: dict[str, Decimal] = {}
    returns: list[float] = []
    for i in range(warmup, n - 1):  # need bar i+1 for the forward return + the day-i+1 funding
        cost_i = Decimal(0)
        if (i - warmup) % cfg.holding_period == 0:
            target = strategy.target_weights({s: funding[s][: i + 1] for s in funding})
            turnover = sum(
                (abs(target.get(s, Decimal(0)) - held.get(s, Decimal(0))) for s in target | held),
                Decimal(0),
            )
            cost_i = turnover * cost
            held = target
        price_return = sum(
            (
                held[s] * (closes[s][i + 1] / closes[s][i] - Decimal(1))
                for s in held
                if closes[s][i] > 0
            ),
            Decimal(0),
        )
        # carry earned holding i->i+1: a short (w<0) receives positive funding, a long pays it.
        carry = sum((-held[s] * funding[s][i + 1] for s in held), Decimal(0))
        returns.append(float(price_return + carry - cost_i))
    return returns


class FundingPanelBacktester:
    """A ``discovery.Backtester`` that runs a funding-carry proposal over a panel.

    Both sources are injected: ``panel_bars_for`` (prices, for the price P&L + the timeline) and
    ``panel_funding_for`` (funding, for the carry signal + P&L). ``cost_fraction`` overrides the
    per-market turnover cost (tests pin it; ``None`` derives it from ``costs.yaml``)."""

    def __init__(
        self,
        *,
        panel_bars_for: PanelBarsFor,
        panel_funding_for: PanelFundingFor,
        min_bars: int | None = None,
        cost_fraction: Decimal | None = None,
    ) -> None:
        self._panel_bars_for = panel_bars_for
        self._panel_funding_for = panel_funding_for
        self._min_bars = min_bars if min_bars is not None else 2 * load_rigor_config().cpcv.n_groups
        self._cost_fraction = cost_fraction

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        """Build the carry strategy, align the panel's prices + funding, and return the per-bar
        dollar-neutral carry return series. Raises ``ValueError`` for an unknown carry template or a
        panel with too few aligned bars for the rigor gate."""
        template = FUNDING_TEMPLATES.get(proposal.template)
        if template is None:
            raise ValueError(
                f"unknown carry template {proposal.template!r}; known: {sorted(FUNDING_TEMPLATES)}"
            )
        strategy = cast(PanelStrategy, template.build(proposal.params))
        bars = self._panel_bars_for(proposal.market, proposal.window)
        funding = self._panel_funding_for(proposal.market, proposal.window)
        # daily_funding buckets to UTC days, so the carry backtest assumes a daily panel — fail loud
        # on an intraday one (funding would land only on each day's 00:00 bar, undercounted).
        sample = next((bar for series in bars.values() for bar in series), None)
        if sample is not None and sample.interval != timedelta(days=1):
            raise NotImplementedError(
                f"funding carry assumes a daily panel (funding aggregates to UTC days); got "
                f"interval {sample.interval} for {proposal.market.value}/{proposal.window}"
            )
        timeline, closes = align_closes(bars)
        # daily-aggregate each member's funding, aligned to the price timeline (0 if a day has
        # none); only members with funding enter the cross-section.
        funding_daily = {sym: daily_funding(rates) for sym, rates in funding.items()}
        funding_aligned = {
            sym: [funding_daily[sym].get(day, Decimal(0)) for day in timeline]
            for sym in closes
            if sym in funding_daily
        }
        n_returns = max(len(timeline) - 1 - strategy.config.lookback, 0)
        if n_returns < self._min_bars:
            raise ValueError(
                f"too few aligned in-sample bars for panel {proposal.market.value}/"
                f"{proposal.window}: {n_returns} returns < {self._min_bars} (the rigor floor)"
            )
        cost = (
            self._cost_fraction
            if self._cost_fraction is not None
            else turnover_cost_fraction(proposal.market)
        )
        return _simulate_carry(strategy, closes, funding_aligned, len(timeline), cost)


class ColdStoreFundingFor:
    """A ``PanelFundingFor`` backed by a :class:`~alpha_core.data.funding.FundingStore` — reads each
    panel member's funding history (the funding analogue of ``ColdStorePanelBarsFor``). Un-ingested
    members read back empty and are dropped (the backtester's ``>= 2*n_groups`` guard catches a thin
    panel). The store's *root* is the isolation boundary: in-sample reads the research-side funding,
    the holdout source the gate-only funding (the structural seal lands with the holdout read)."""

    def __init__(self, store: FundingStore, panels: Mapping[CellKey, list[SeriesCoord]]) -> None:
        self._store = store
        self._panels: dict[CellKey, list[SeriesCoord]] = dict(panels)

    @classmethod
    def from_config(cls, store: FundingStore) -> ColdStoreFundingFor:
        """Build the panel -> member-series map from ``config/discovery.yaml`` (funding has no
        interval; ``SeriesCoord``'s interval is carried but unused by the funding read)."""
        cfg = load_discovery_config()
        panels = {
            (panel.market, panel.name): [
                SeriesCoord(symbol, panel.venue, panel.interval_seconds) for symbol in panel.symbols
            ]
            for panel in cfg.panels
        }
        return cls(store, panels)

    def __call__(self, market: AssetClass, window: str) -> dict[str, list[FundingRate]]:
        coords = self._panels.get((market, window))
        if coords is None:
            known = sorted(f"{m.value}/{w}" for m, w in self._panels)
            raise ValueError(
                f"no discovery panel mapped for {market.value}/{window!r}; known panels: {known}."
            )
        members: dict[str, list[FundingRate]] = {}
        for coord in coords:
            rates = self._store.read(symbol=coord.symbol, venue=coord.venue)
            if rates:
                members[coord.symbol] = rates
        return members
