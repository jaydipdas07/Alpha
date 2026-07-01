"""The cross-sectional panel backtester (M3.0) — the ``discovery.Backtester`` for *panels*.

The single-instrument ``EngineBacktester`` runs one strategy through the order-based engine on one
symbol's bars. A cross-sectional strategy is a different computation: each rebalance it ranks the
whole panel and holds a dollar-neutral long-top / short-bottom book, so its performance is a fold
over **portfolio** returns, not one instrument's fills. This module computes that fold directly —
weights at bar ``t`` (from data up to ``t``) times the realised ``t -> t+1`` returns, plus the
funding the perp book pays/receives (when a funding source is wired), net of a turnover cost — and
hands the per-bar return series to the **unchanged** rigor gate (DSR / CPCV / quant-analyst). It
fits the ``discovery.Backtester`` shape, so ``run_discovery_cycle`` drives it like the
single-instrument one.

**Signal-quality, not deployment parity.** Like single-instrument discovery (which tests at 1-unit
size, not capital-scaled deployment), this measures the *signal*: a dollar-neutral book (longs sum
``+1``, shorts ``-1``; Sharpe is scale-invariant) with a coarse, config-driven turnover cost. A
survivor's live parity is the live portfolio path's job (``WeightAllocator``), deferred exactly as a
single-instrument survivor's live parity is — discovery's job is to find a real signal first.

**Look-ahead-clean (TEST-1).** ``target_weights`` at bar ``i`` is fed the trailing ``lookback + 1``
window ending at ``i`` (all a trailing score consumes — never a future slot), and the return it
earns is the *next* bar's; the timeline is bar-indexed (no wall-clock). The panel is **unbalanced**
(point-in-time membership): the timeline is the union of every member's bar-starts, and a symbol is
scoreable at a rebalance only once its whole trailing window is real data — no forward-fill, no
survivorship truncation to the youngest listing. **Holdout isolation (TEST-3)** is delegated to the
injected ``panel_bars_for`` boundary (the no-ACL sealed cold store), exactly as
``EngineBacktester`` delegates to ``bars_for`` — this module adds no filter.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast

from alpha_core.core.enums import AssetClass
from alpha_core.core.models import Bar
from alpha_core.data.funding import FundingRate, daily_funding
from alpha_core.data.store import BarStore
from alpha_core.helpers.config import load_discovery_config, load_rigor_config, load_yaml
from alpha_core.research.cold_store_bars import CellKey, SeriesCoord
from alpha_core.research.strategist import IntRange, StrategyProposal, StrategyTemplate
from alpha_core.strategy.examples.cross_sectional import (
    BetaNeutralConfig,
    BetaNeutralMomentum,
    CrossSectionalConfig,
    CrossSectionalMomentum,
    CrossSectionalReversal,
    PanelStrategy,
    VolScaledMomentum,
    VolScaledReversal,
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
    # Richer BOOK CONSTRUCTIONS over the same rank signals (the M3.0-follow-on re-run): inverse-vol
    # leg weights (risk parity in the leg) and a benchmark-beta hedge. Same param grids as their
    # base templates — no new tunables, so the n_trials deflation stays honest.
    "cross_sectional_momentum_volscaled": StrategyTemplate(
        "cross_sectional_momentum_volscaled",
        "cross_sectional_momentum_volscaled",
        CrossSectionalConfig,
        VolScaledMomentum,
        {"lookback": IntRange(5, 60), "top_k": IntRange(2, 8), "holding_period": IntRange(1, 10)},
    ),
    "cross_sectional_reversal_volscaled": StrategyTemplate(
        "cross_sectional_reversal_volscaled",
        "cross_sectional_reversal_volscaled",
        CrossSectionalConfig,
        VolScaledReversal,
        {"lookback": IntRange(2, 30), "top_k": IntRange(2, 8), "holding_period": IntRange(1, 10)},
    ),
    "cross_sectional_momentum_betaneutral": StrategyTemplate(
        "cross_sectional_momentum_betaneutral",
        "cross_sectional_momentum_betaneutral",
        BetaNeutralConfig,
        BetaNeutralMomentum,
        {"lookback": IntRange(5, 60), "top_k": IntRange(2, 8), "holding_period": IntRange(1, 10)},
    ),
}

# A panel as the bars seam sees it: (market, panel_name) -> {symbol: in-sample bars}. CONTRACT (like
# engine_backtester.BarsFor): must return in-sample-only bars; isolation is structural at the store.
PanelBarsFor = Callable[[AssetClass, str], dict[str, list[Bar]]]

# (market, panel_name) -> {symbol: funding history}. CONTRACT: in-sample-only, like PanelBarsFor.
# Lives here (not funding_backtester) so PanelBacktester can take one without an import cycle.
PanelFundingFor = Callable[[AssetClass, str], dict[str, list[FundingRate]]]


def turnover_cost_fraction(market: AssetClass) -> Decimal:
    """Per-unit-turnover cost (fee + slippage, a fraction) for ``market``, from ``costs.yaml`` — the
    coarse cost the cross-sectional signal must clear (it rebalances, so turnover bites). Crypto
    panels are perp universes, so they pay the ``crypto_perp`` regime (perp taker + perp slippage —
    NOT the spot fee + TDS the old single ``crypto`` segment conflated, ~2.5x real perp costs); a
    non-crypto panel's fuller cost model (equity brokerage/STT/GST) is a follow-up, so it raises
    rather than silently under-charge."""
    costs = load_yaml("costs.yaml")
    if market is not AssetClass.CRYPTO:
        raise NotImplementedError(
            f"turnover cost for {market.value} panels is a follow-up; only crypto is modelled"
        )
    slippage_bps = Decimal(str(costs["slippage"]["crypto_perp"]["value"]))
    fee = Decimal(str(costs["segments"]["crypto_perp"]["trading_fee"]["pct"]))
    return slippage_bps / Decimal(10000) + fee


def align_closes(
    panel: Mapping[str, list[Bar]],
) -> tuple[list[datetime], dict[str, list[Decimal | None]]]:
    """**Union**-join the panel's member series on bar-start: the timeline is every bar-start any
    member printed, and each member's closes are aligned to it with ``None`` where it has no bar
    (not yet listed / a gap / delisted). An *unbalanced* panel — point-in-time membership — so a
    2019-anchored timeline keeps the full history of the early listings instead of truncating the
    whole panel to the youngest member (the old inner-join wasted more than half the free span).
    No forward-fill (look-ahead-free): a missing close stays ``None`` and the fold's membership
    rule decides eligibility per bar. Members with no bars at all are dropped (an un-ingested
    member is simply out of the cross-section)."""
    by_symbol = {sym: {b.start: b.close for b in bars} for sym, bars in panel.items() if bars}
    if not by_symbol:
        return [], {}
    timeline = sorted(set().union(*(stamps.keys() for stamps in by_symbol.values())))
    closes: dict[str, list[Decimal | None]] = {
        sym: [stamps.get(ts) for ts in timeline] for sym, stamps in by_symbol.items()
    }
    return timeline, closes


def align_daily_funding(
    bars: Mapping[str, list[Bar]],
    funding: Mapping[str, list[FundingRate]],
    timeline: Sequence[datetime],
) -> dict[str, list[Decimal]]:
    """Daily-aggregate each member's funding and align it to the panel ``timeline`` (0 for a day
    with no funding row; a member with no funding history at all is simply absent). Funding
    aggregates to UTC days (``daily_funding``), so a **daily** panel is required — fail loud on an
    intraday one (funding would land only on each day's 00:00 bar, undercounted)."""
    sample = next((bar for series in bars.values() for bar in series), None)
    if sample is not None and sample.interval != timedelta(days=1):
        raise NotImplementedError(
            f"funding assumes a daily panel (funding aggregates to UTC days); got "
            f"interval {sample.interval}"
        )
    per_day = {sym: daily_funding(rates) for sym, rates in funding.items()}
    return {sym: [days.get(day, Decimal(0)) for day in timeline] for sym, days in per_day.items()}


def _real_window(series: Sequence[Decimal | None], lo: int, hi: int) -> bool:
    """True iff every slot of ``series[lo:hi]`` is a real value — the point-in-time membership
    test (a symbol enters the cross-section only once it has printed the full trailing window)."""
    return all(series[j] is not None for j in range(lo, hi))


def simulate_panel(
    strategy: PanelStrategy,
    closes: Mapping[str, Sequence[Decimal | None]],
    signal: Mapping[str, Sequence[Decimal | None]],
    funding: Mapping[str, Sequence[Decimal]],
    n: int,
    cost: Decimal,
) -> list[float]:
    """The dollar-neutral rebalanced-portfolio fold shared by every panel backtester: re-rank every
    ``holding_period`` bars (charging turnover at the re-rank), hold the target weights between,
    and earn each bar's cross-sectional price return PLUS the funding transfer, net of turnover.

    ``signal`` is the per-symbol series ``target_weights`` ranks on — the closes for a price
    template, the aligned funding for a carry template. ``funding`` is the timeline-aligned daily
    funding the held book pays/receives (a short, ``w < 0``, *receives* positive funding; a long
    pays it); pass ``{}`` for a funding-blind fold. ``float`` only at the boundary (the statistics
    plane).

    **Point-in-time membership (the unbalanced panel):** the aligned series may hold ``None``
    where a member has no bar (not yet listed / a gap / delisted). A symbol is *scoreable* at a
    rebalance only if both its closes and its signal are real over the whole trailing
    ``lookback + 1`` window (it earns a rank only on real consecutive data — never on a fill); the
    strategy is handed exactly that window, which is all a trailing score consumes. A held symbol
    earns the ``i -> i+1`` return only when both closes are real (else it contributes a flat 0
    that bar), and one whose data stops is dropped at the next rebalance (turnover charged)."""
    cfg = strategy.config
    warmup = cfg.lookback  # the first bar with a full trailing window
    held: dict[str, Decimal] = {}
    returns: list[float] = []
    for i in range(warmup, n - 1):  # need bar i+1 for the forward return + the day-i+1 funding
        cost_i = Decimal(0)
        if (i - warmup) % cfg.holding_period == 0:
            lo = i - warmup  # the trailing lookback+1 window [lo, i] a trailing score consumes
            eligible = {
                s: cast("Sequence[Decimal]", signal[s][lo : i + 1])
                for s in signal
                if _real_window(closes[s], lo, i + 1) and _real_window(signal[s], lo, i + 1)
            }
            target = strategy.target_weights(eligible)
            turnover = sum(
                (abs(target.get(s, Decimal(0)) - held.get(s, Decimal(0))) for s in target | held),
                Decimal(0),
            )
            cost_i = turnover * cost
            held = target
        # a held symbol earns only across two REAL closes; a non-positive current close (a data
        # artifact, not a real -100%) also contributes a flat 0 — guards the division, mirroring
        # the base<=0 score guard.
        bar_return = sum(
            (
                held[s] * (c1 / c0 - Decimal(1))
                for s in held
                if (c0 := closes[s][i]) is not None
                and c0 > 0
                and (c1 := closes[s][i + 1]) is not None
            ),
            Decimal(0),
        )
        # funding transferred holding i->i+1: a short (w<0) receives positive funding, a long
        # pays it. A member with no funding series contributes 0 (nothing known to transfer).
        carry = sum(
            (-held[s] * f[i + 1] for s in held if (f := funding.get(s)) is not None),
            Decimal(0),
        )
        returns.append(float(bar_return + carry - cost_i))
    return returns


class PanelBacktester:
    """A ``discovery.Backtester`` that runs a cross-sectional proposal over a panel's bars.

    The ``panel_bars_for`` source is injected (production reads the cold store; tests inject
    synthetic panels). ``panel_funding_for`` (optional) injects the members' funding history: a
    dollar-neutral **perp** book pays/earns funding continuously (momentum books typically bleed
    it), so production perp panels pass it and the fold adds the transfer to the price P&L; omit it
    only for a funding-free panel (e.g. a future equity one). ``cost_fraction`` overrides the
    per-market turnover cost — tests pin it, ``None`` derives it from ``costs.yaml``."""

    def __init__(
        self,
        *,
        panel_bars_for: PanelBarsFor,
        panel_funding_for: PanelFundingFor | None = None,
        min_bars: int | None = None,
        cost_fraction: Decimal | None = None,
    ) -> None:
        self._panel_bars_for = panel_bars_for
        self._panel_funding_for = panel_funding_for
        # the rigor gate needs >= 2*n_groups return observations; fail fast on a too-short panel.
        self._min_bars = min_bars if min_bars is not None else 2 * load_rigor_config().cpcv.n_groups
        self._cost_fraction = cost_fraction

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        """Build the cross-sectional strategy, align the panel's in-sample bars (+ funding when a
        source is wired), and return the per-bar dollar-neutral portfolio return series. Raises
        ``ValueError`` for an unknown panel template or a panel with too few aligned bars for the
        rigor gate."""
        template = PANEL_TEMPLATES.get(proposal.template)
        if template is None:
            raise ValueError(
                f"unknown panel template {proposal.template!r}; known: {sorted(PANEL_TEMPLATES)}"
            )
        strategy = cast(PanelStrategy, template.build(proposal.params))
        panel = self._panel_bars_for(proposal.market, proposal.window)
        timeline, closes = align_closes(panel)
        funding_aligned: dict[str, list[Decimal]] = {}
        if self._panel_funding_for is not None:
            funding = self._panel_funding_for(proposal.market, proposal.window)
            funding_aligned = align_daily_funding(panel, funding, timeline)
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
        return simulate_panel(strategy, closes, closes, funding_aligned, len(timeline), cost)


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
            if not bars:
                continue
            if bars[0].asset_class != market:  # a config mismap would cost the wrong market's rate
                raise ValueError(
                    f"discovery panel {market.value}/{window!r} member {coord.symbol} is "
                    f"{bars[0].asset_class.value}, not {market.value} — fix config/discovery.yaml."
                )
            members[coord.symbol] = bars
        return members
