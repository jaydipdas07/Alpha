"""The cross-sectional panel backtester (M3.0) — the ``discovery.Backtester`` for *panels*.

The single-instrument ``EngineBacktester`` runs one strategy through the order-based engine on one
symbol's bars. A cross-sectional strategy is a different computation: each rebalance it ranks the
whole panel and holds a dollar-neutral long-top / short-bottom book, so its performance is a fold
over **portfolio** returns, not one instrument's fills. This module computes that fold directly —
weights at bar ``t`` (from data up to ``t``) times the realised ``t -> t+1`` returns, net of a
turnover cost — and hands the per-bar return series to the **unchanged** rigor gate (DSR / CPCV /
quant-analyst). It fits the ``discovery.Backtester`` shape, so ``run_discovery_cycle`` drives it
like the single-instrument one.

**Signal-quality, not deployment parity.** Like single-instrument discovery (which tests at 1-unit
size, not capital-scaled deployment), this measures the *signal*: a dollar-neutral book (longs sum
``+1``, shorts ``-1``; Sharpe is scale-invariant) with a coarse, config-driven turnover cost. A
survivor's live parity is the live portfolio path's job (``WeightAllocator``), deferred exactly as a
single-instrument survivor's live parity is — discovery's job is to find a real signal first.

**Look-ahead-clean (TEST-1).** ``target_weights`` at bar ``i`` is fed ``closes[: i + 1]`` only, and
the return it earns is the *next* bar's; the timeline is bar-indexed (no wall-clock). **Holdout
isolation (TEST-3)** is delegated to the injected ``panel_bars_for`` boundary (the no-ACL sealed
cold store), exactly as ``EngineBacktester`` delegates to ``bars_for`` — this module adds no filter.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import cast

from alpha_core.core.enums import AssetClass
from alpha_core.core.models import Bar
from alpha_core.data.store import BarStore
from alpha_core.helpers.config import load_discovery_config, load_rigor_config, load_yaml
from alpha_core.research.cold_store_bars import CellKey, SeriesCoord
from alpha_core.research.strategist import IntRange, StrategyProposal, StrategyTemplate
from alpha_core.strategy.examples.cross_sectional import (
    CrossSectionalConfig,
    CrossSectionalMomentum,
    CrossSectionalReversal,
    PanelStrategy,
)

# The cross-sectional template registry (kept separate from the single-instrument ``TEMPLATES`` so
# the single-instrument nightly / promote / tests are untouched). Reversal uses a shorter lookback
# (short-term reversal) than momentum. ``top_k <= 8`` keeps ``2*top_k`` within any real panel.
PANEL_TEMPLATES: dict[str, StrategyTemplate] = {
    "cross_sectional_momentum": StrategyTemplate(
        "cross_sectional_momentum",
        "cross_sectional_momentum",
        CrossSectionalConfig,
        CrossSectionalMomentum,
        {"lookback": IntRange(5, 60), "top_k": IntRange(2, 8), "holding_period": IntRange(1, 10)},
    ),
    "cross_sectional_reversal": StrategyTemplate(
        "cross_sectional_reversal",
        "cross_sectional_reversal",
        CrossSectionalConfig,
        CrossSectionalReversal,
        {"lookback": IntRange(2, 30), "top_k": IntRange(2, 8), "holding_period": IntRange(1, 10)},
    ),
}

# A panel as the bars seam sees it: (market, panel_name) -> {symbol: in-sample bars}. CONTRACT (like
# engine_backtester.BarsFor): must return in-sample-only bars; isolation is structural at the store.
PanelBarsFor = Callable[[AssetClass, str], dict[str, list[Bar]]]


def turnover_cost_fraction(market: AssetClass) -> Decimal:
    """Per-unit-turnover cost (fee + slippage, a fraction) for ``market``, from ``costs.yaml`` — the
    coarse cost the cross-sectional signal must clear (it rebalances, so turnover bites). Crypto =
    taker fee + slippage; a non-crypto panel's fuller cost model (equity brokerage/STT/GST) is a
    follow-up, so it raises rather than silently under-charge."""
    costs = load_yaml("costs.yaml")
    if market is not AssetClass.CRYPTO:
        raise NotImplementedError(
            f"turnover cost for {market.value} panels is a follow-up; only crypto is modelled"
        )
    slippage_bps = Decimal(str(costs["slippage"]["crypto"]["value"]))
    fee = Decimal(str(costs["segments"]["crypto"]["trading_fee"]["pct"]))
    return slippage_bps / Decimal(10000) + fee


def _align_closes(
    panel: Mapping[str, list[Bar]],
) -> tuple[list[datetime], dict[str, list[Decimal]]]:
    """Inner-join the panel's member series on bar-start, returning the common timeline and each
    member's closes aligned to it. Inner-join is the look-ahead-free choice (no forward-fill); for a
    rectangular panel it keeps every bar. Members with no bars are dropped (an un-ingested member is
    simply out of the cross-section)."""
    by_symbol = {sym: {b.start: b.close for b in bars} for sym, bars in panel.items() if bars}
    if not by_symbol:
        return [], {}
    common = set.intersection(*(set(stamps) for stamps in by_symbol.values()))
    timeline = sorted(common)
    closes = {sym: [stamps[ts] for ts in timeline] for sym, stamps in by_symbol.items()}
    return timeline, closes


def _simulate(
    strategy: PanelStrategy, closes: Mapping[str, list[Decimal]], n: int, cost: Decimal
) -> list[float]:
    """The dollar-neutral rebalanced-portfolio fold: re-rank every ``holding_period`` bars (charging
    turnover at the re-rank), hold the target weights between, and earn each bar's cross-sectional
    return. ``float`` only at the boundary (the statistics plane)."""
    cfg = strategy.config
    warmup = cfg.lookback  # the first bar with a full trailing-return window
    held: dict[str, Decimal] = {}
    returns: list[float] = []
    for i in range(warmup, n - 1):  # need bar i+1 for the forward return
        cost_i = Decimal(0)
        if (i - warmup) % cfg.holding_period == 0:
            target = strategy.target_weights({s: closes[s][: i + 1] for s in closes})
            turnover = sum(
                (abs(target.get(s, Decimal(0)) - held.get(s, Decimal(0))) for s in target | held),
                Decimal(0),
            )
            cost_i = turnover * cost
            held = target
        bar_return = sum(
            (held[s] * (closes[s][i + 1] / closes[s][i] - Decimal(1)) for s in held),
            Decimal(0),
        )
        returns.append(float(bar_return - cost_i))
    return returns


class PanelBacktester:
    """A ``discovery.Backtester`` that runs a cross-sectional proposal over a panel's bars.

    The ``panel_bars_for`` source is injected (production reads the cold store; tests inject
    synthetic panels). ``cost_fraction`` overrides the per-market turnover cost — tests pin it,
    ``None`` derives it from ``costs.yaml``."""

    def __init__(
        self,
        *,
        panel_bars_for: PanelBarsFor,
        min_bars: int | None = None,
        cost_fraction: Decimal | None = None,
    ) -> None:
        self._panel_bars_for = panel_bars_for
        # the rigor gate needs >= 2*n_groups return observations; fail fast on a too-short panel.
        self._min_bars = min_bars if min_bars is not None else 2 * load_rigor_config().cpcv.n_groups
        self._cost_fraction = cost_fraction

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        """Build the cross-sectional strategy, align the panel's in-sample bars, and return the
        per-bar dollar-neutral portfolio return series. Raises ``ValueError`` for an unknown panel
        template or a panel with too few aligned bars for the rigor gate."""
        template = PANEL_TEMPLATES.get(proposal.template)
        if template is None:
            raise ValueError(
                f"unknown panel template {proposal.template!r}; known: {sorted(PANEL_TEMPLATES)}"
            )
        strategy = cast(PanelStrategy, template.build(proposal.params))
        panel = self._panel_bars_for(proposal.market, proposal.window)
        timeline, closes = _align_closes(panel)
        # one return per bar from `lookback` to the second-to-last aligned bar.
        n_returns = max(len(timeline) - 1 - strategy.config.lookback, 0)
        if n_returns < self._min_bars:
            raise ValueError(
                f"too few aligned in-sample bars for panel {proposal.market.value}/"
                f"{proposal.window}: {n_returns} returns < {self._min_bars} "
                "(need >= 2*cpcv.n_groups; widen the window or lower lookback)"
            )
        cost = (
            self._cost_fraction
            if self._cost_fraction is not None
            else turnover_cost_fraction(proposal.market)
        )
        return _simulate(strategy, closes, len(timeline), cost)


class ColdStorePanelBarsFor:
    """A ``PanelBarsFor`` backed by the no-ACL cold store — the production panel boundary (the panel
    analogue of ``ColdStoreBarsFor``). Resolves ``(market, panel_name)`` to its member series and
    reads each back as in-sample bars. A pure pass-through; isolation is structural at the store."""

    def __init__(self, store: BarStore, panels: Mapping[CellKey, list[SeriesCoord]]) -> None:
        self._store = store
        self._panels: dict[CellKey, list[SeriesCoord]] = dict(panels)

    @classmethod
    def from_config(cls, store: BarStore) -> ColdStorePanelBarsFor:
        """Build the panel -> member-series map from ``config/discovery.yaml`` over ``store``."""
        cfg = load_discovery_config()
        panels = {
            (panel.market, panel.name): [
                SeriesCoord(symbol, panel.venue, panel.interval_seconds) for symbol in panel.symbols
            ]
            for panel in cfg.panels
        }
        return cls(store, panels)

    def __call__(self, market: AssetClass, window: str) -> dict[str, list[Bar]]:
        coords = self._panels.get((market, window))
        if coords is None:
            known = sorted(f"{m.value}/{w}" for m, w in self._panels)
            raise ValueError(
                f"no discovery panel mapped for {market.value}/{window!r}; known panels: {known}. "
                "Add it to config/discovery.yaml."
            )
        # un-ingested members read back empty and are dropped from the cross-section; the
        # backtester's >= 2*n_groups guard surfaces a panel that is too thin overall.
        members: dict[str, list[Bar]] = {}
        for coord in coords:
            bars = self._store.read_bars(
                symbol=coord.symbol, venue=coord.venue, interval_seconds=coord.interval_seconds
            )
            if bars:
                members[coord.symbol] = bars
        return members
