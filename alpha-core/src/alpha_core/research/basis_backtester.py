"""The delta-neutral basis-carry backtester (M3.0 basis track) — the ``discovery.Backtester`` for
the TWO-LEG book.

Every prior family bets on price direction (and the M3.0 re-run showed none of those bets clears
the holdout). The basis book doesn't: for each selected symbol it holds **short one unit of perp,
long one unit of spot** — the price legs cancel per name, and what remains is the funding a short
perp *receives* (crypto's structural carry), minus the basis wiggle between the two markets and
the two-leg trading costs:

    return[i->i+1] = Σ w·[ (spot_ret - perp_ret)  +  funding on day i+1 ]  -  turnover·(both legs)

The signal is deliberately structural, not mined: rank by trailing-mean funding, hold only
positive-carry names (``BasisCarry``), one small param grid — so the DSR deflation stays light and
the verdict hinges on whether the harvested funding clears honest two-leg costs, not on parameter
luck. The per-bar series feeds the **unchanged** rigor gate.

**Look-ahead-clean (TEST-1):** weights at bar ``i`` use funding up to ``i``; the basis move and
the carry are both over ``i -> i+1``; both legs are aligned to ONE shared union timeline so their
bars line up by index, and a name is scoreable only where BOTH legs printed the full trailing
window (point-in-time membership, per leg). **Holdout isolation (TEST-3)** is delegated to the
injected ``panel_bars_for`` boundary — both legs read through the SAME boundary object (research
or gate-only), wired in one tested place (``promote.build_basis_backtesters``), so the two legs
can never straddle the seal.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Protocol, cast, runtime_checkable

from alpha_core.core.enums import AssetClass
from alpha_core.helpers.config import load_discovery_config, load_rigor_config, load_yaml
from alpha_core.research.cold_store_bars import CellKey
from alpha_core.research.panel_backtester import (
    PanelBarsFor,
    PanelFundingFor,
    align_daily_funding,
    closes_on,
    real_window,
    turnover_cost_fraction,
)
from alpha_core.research.strategist import (
    DecimalRange,
    IntRange,
    StrategyProposal,
    StrategyTemplate,
)
from alpha_core.strategy.examples.cross_sectional import (
    BasisCarry,
    BasisCarryHold,
    BasisHoldConfig,
    CrossSectionalConfig,
    PanelStrategy,
)


@runtime_checkable
class HoldAwareStrategy(Protocol):
    """A basis strategy whose next book depends on the CURRENTLY-HELD one (hysteresis): the fold
    passes the held weights back in, so retention thresholds can differ from entry thresholds.
    The strategy object itself stays pure — all state lives in the fold's ``held``."""

    @property
    def config(self) -> CrossSectionalConfig: ...

    def rebalance(
        self, signal: Mapping[str, Sequence[Decimal]], held: Mapping[str, Decimal]
    ) -> dict[str, Decimal]: ...


# The basis template registry (its own backtester — a two-leg fold). `basis_carry` mirrors the
# carry family's grid (no new tunables); `basis_carry_hold` — the LOW-CHURN follow-on — searches a
# deliberately TINY pre-registered space (lookback x entry rate = 24 configs; cadence/breadth/
# hysteresis-fraction fixed in BasisHoldConfig): the signal is structural, so parameter mining is
# not the point and the DSR deflation stays light.
BASIS_TEMPLATES: dict[str, StrategyTemplate] = {
    "basis_carry": StrategyTemplate(
        "basis_carry",
        "basis_carry",
        CrossSectionalConfig,
        BasisCarry,
        {"lookback": IntRange(3, 30), "top_k": IntRange(2, 8), "holding_period": IntRange(1, 10)},
    ),
    "basis_carry_hold": StrategyTemplate(
        "basis_carry_hold",
        "basis_carry_hold",
        BasisHoldConfig,
        BasisCarryHold,
        {
            "lookback": IntRange(7, 14),
            "entry_rate_annual": DecimalRange(Decimal("0.05"), Decimal("0.15"), Decimal("0.05")),
        },
    ),
}


def spot_turnover_cost_fraction() -> Decimal:
    """Per-unit-turnover cost for the SPOT hedge leg (``crypto_spot`` fee + slippage, a fraction).

    **TDS is deliberately excluded from the research fold:** India's 1% TDS on VDA sells is a
    *creditable withholding* (recoverable against tax due), not a trading fee — charging it as a
    cost would double-count it against the operator's tax computation. It IS a real deploy-time
    cash-flow drag, and (with the 30%/no-loss-offset VDA regime) the reason a dated-future hedge
    leg — a derivative, no VDA transfer — may replace spot at deployment: a [You]/CA call, out of
    research scope. The signal read here is leg-structure-agnostic."""
    costs = load_yaml("costs.yaml")
    slippage_bps = Decimal(str(costs["slippage"]["crypto_spot"]["value"]))
    fee = Decimal(str(costs["segments"]["crypto_spot"]["trading_fee"]["pct"]))
    return slippage_bps / Decimal(10000) + fee


def basis_cost_fraction(market: AssetClass, scenario: str = "taker") -> Decimal:
    """The COMBINED both-leg per-unit-of-book-turnover cost under an execution ``scenario``
    (one unit of basis turnover trades one unit of notional on EACH leg).

    - ``"taker"`` — crossing the spread on both legs: (perp fee + slippage) + (spot fee +
      slippage). The deployable-today assumption; the primary gate scenario.
    - ``"maker"`` — post-only resting fills on both legs: the ``maker_fee`` rates only, no
      crossing slippage. Adverse selection and unfilled-resting risk are unmodelled — the
      mandatory 2x stress multiplier is the margin — so a maker-only survivor is a statement
      about a maker-execution DEPLOYMENT, which the worker cannot yet do (Phase-4+ ability).
    """
    if market is not AssetClass.CRYPTO:
        raise NotImplementedError(
            f"basis cost for {market.value} is a follow-up; only crypto legs are modelled"
        )
    if scenario == "taker":
        return turnover_cost_fraction(market) + spot_turnover_cost_fraction()
    if scenario == "maker":
        costs = load_yaml("costs.yaml")
        perp = Decimal(str(costs["segments"]["crypto_perp"]["maker_fee"]["pct"]))
        spot = Decimal(str(costs["segments"]["crypto_spot"]["maker_fee"]["pct"]))
        return perp + spot
    raise ValueError(f"unknown basis cost scenario {scenario!r}; known: taker, maker")


def _real_pair(series: Sequence[Decimal | None], i: int) -> tuple[Decimal, Decimal] | None:
    """``(close[i], close[i+1])`` when both are real and the base is positive, else ``None`` (a
    data gap / a non-positive artifact — the leg contributes a flat 0 that bar, never a fill)."""
    c0 = series[i]
    c1 = series[i + 1]
    if c0 is None or c1 is None or c0 <= 0:
        return None
    return c0, c1


def simulate_basis(
    strategy: PanelStrategy | HoldAwareStrategy,
    perp_closes: Mapping[str, Sequence[Decimal | None]],
    spot_closes: Mapping[str, Sequence[Decimal | None]],
    funding: Mapping[str, Sequence[Decimal]],
    n: int,
    cost: Decimal,
) -> list[float]:
    """The two-leg basis fold: re-rank by trailing funding every ``holding_period`` bars (charging
    ``cost`` — the COMBINED both-leg rate — per unit of book turnover, since one unit of basis
    turnover trades one unit of notional on EACH leg), hold the book between, and earn each bar's
    basis move plus the carry.

    Per held name: ``w·[(s1/s0 - p1/p0) + funding[i+1]]`` — long spot + short perp nets to the
    spread of the two legs' returns (the price direction cancels), and a short perp *receives*
    positive funding. **Point-in-time membership per leg:** a name is scoreable at a rebalance
    only if BOTH legs' closes are real over the whole trailing ``lookback + 1`` window; a held
    name whose either leg goes missing mid-hold contributes a flat 0 that bar (it cannot be
    marked) and is dropped at the next re-rank (turnover charged). ``float`` only at the boundary
    (the statistics plane)."""
    cfg = strategy.config
    warmup = cfg.lookback  # the first bar with a full trailing-funding window
    held: dict[str, Decimal] = {}
    returns: list[float] = []
    for i in range(warmup, n - 1):  # need bar i+1 for the basis move + the day-i+1 funding
        cost_i = Decimal(0)
        if (i - warmup) % cfg.holding_period == 0:
            lo = i - warmup  # the trailing lookback+1 window [lo, i] the funding score consumes
            eligible = {
                s: funding[s][lo : i + 1]
                for s in funding
                if s in perp_closes
                and s in spot_closes
                and real_window(perp_closes[s], lo, i + 1)
                and real_window(spot_closes[s], lo, i + 1)
            }
            # a hold-aware (hysteresis) strategy sees the held book too; a plain one re-selects.
            if isinstance(strategy, HoldAwareStrategy):
                target = strategy.rebalance(eligible, held)
            else:
                target = strategy.target_weights(eligible)
            turnover = sum(
                (abs(target.get(s, Decimal(0)) - held.get(s, Decimal(0))) for s in target | held),
                Decimal(0),
            )
            cost_i = turnover * cost
            held = target
        bar_return = Decimal(0)
        for symbol, weight in held.items():
            perp = _real_pair(perp_closes[symbol], i)
            spot = _real_pair(spot_closes[symbol], i)
            if perp is None or spot is None:
                continue  # a leg's data stopped mid-hold: flat 0 this bar, dropped at re-rank
            p0, p1 = perp
            s0, s1 = spot
            basis_move = s1 / s0 - p1 / p0  # (long-spot return) + (short-perp return)
            bar_return += weight * (basis_move + funding[symbol][i + 1])
        returns.append(float(bar_return - cost_i))
    return returns


def _basis_cells_from_config() -> dict[CellKey, tuple[str, str]]:
    """The ``(market, basis-cell-name) -> (perp_panel, spot_panel)`` map from ``discovery.yaml``
    (leg existence + market agreement are validated at config load)."""
    return {
        (basis.market, basis.name): (basis.perp_panel, basis.spot_panel)
        for basis in load_discovery_config().basis_panels
    }


class BasisPanelBacktester:
    """A ``discovery.Backtester`` that runs a basis-carry proposal over a (perp, spot) panel pair.

    ``proposal.window`` names a configured *basis cell* (``discovery.yaml basis_panels``), mapped
    to its two leg panels; BOTH legs are read through the same injected ``panel_bars_for``
    boundary (the TEST-3 wiring lives in ``promote.build_basis_backtesters``), and the perp leg's
    funding through ``panel_funding_for`` (the signal AND the carry). ``cost_fraction`` overrides
    the combined both-leg per-unit-turnover cost — tests pin it; ``None`` derives it from
    ``costs.yaml`` under ``cost_scenario`` ("taker" crossing, the deployable-today primary;
    "maker" post-only — see :func:`basis_cost_fraction`)."""

    def __init__(
        self,
        *,
        panel_bars_for: PanelBarsFor,
        panel_funding_for: PanelFundingFor,
        basis_cells: Mapping[CellKey, tuple[str, str]] | None = None,
        min_bars: int | None = None,
        cost_fraction: Decimal | None = None,
        cost_scenario: str = "taker",
    ) -> None:
        self._panel_bars_for = panel_bars_for
        self._panel_funding_for = panel_funding_for
        self._basis = dict(basis_cells) if basis_cells is not None else _basis_cells_from_config()
        # the rigor gate needs >= 2*n_groups return observations; fail fast on a too-short panel.
        self._min_bars = min_bars if min_bars is not None else 2 * load_rigor_config().cpcv.n_groups
        self._cost_fraction = cost_fraction
        self._cost_scenario = cost_scenario

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        """Build the basis strategy, align BOTH legs + the funding to one shared union timeline,
        and return the per-bar basis-book return series. Raises ``ValueError`` for an unknown
        basis template / cell or a pair with too few aligned bars for the rigor gate."""
        template = BASIS_TEMPLATES.get(proposal.template)
        if template is None:
            raise ValueError(
                f"unknown basis template {proposal.template!r}; known: {sorted(BASIS_TEMPLATES)}"
            )
        strategy = cast("PanelStrategy | HoldAwareStrategy", template.build(proposal.params))
        legs = self._basis.get((proposal.market, proposal.window))
        if legs is None:
            known = sorted(f"{m.value}/{w}" for m, w in self._basis)
            raise ValueError(
                f"no basis cell mapped for {proposal.market.value}/{proposal.window!r}; known: "
                f"{known}. Add it to config/discovery.yaml basis_panels."
            )
        perp_name, spot_name = legs
        perp_bars = self._panel_bars_for(proposal.market, perp_name)
        spot_bars = self._panel_bars_for(proposal.market, spot_name)
        funding = self._panel_funding_for(proposal.market, perp_name)
        # ONE shared union timeline across BOTH legs, so the legs line up by index; membership is
        # then per-leg per-bar (a name needs both legs real over the trailing window to score).
        timeline = sorted(
            {
                bar.start
                for panel in (perp_bars, spot_bars)
                for series in panel.values()
                for bar in series
            }
        )
        perp_closes = closes_on(perp_bars, timeline)
        spot_closes = closes_on(spot_bars, timeline)
        funding_aligned = align_daily_funding(perp_bars, funding, timeline)
        n_returns = max(len(timeline) - 1 - strategy.config.lookback, 0)
        if n_returns < self._min_bars:
            raise ValueError(
                f"too few aligned in-sample bars for basis {proposal.market.value}/"
                f"{proposal.window}: {n_returns} returns < {self._min_bars} (the rigor floor)"
            )
        cost = (
            self._cost_fraction
            if self._cost_fraction is not None
            else basis_cost_fraction(proposal.market, self._cost_scenario)
        )
        return simulate_basis(
            strategy, perp_closes, spot_closes, funding_aligned, len(timeline), cost
        )
