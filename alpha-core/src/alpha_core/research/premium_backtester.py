"""The G7 premium-dislocation reversion family — MAKER-NATIVE folds over the 1m perp
premium tape (`docs/research/intraday-edge-survey-2026-07-v2.md` round-four addendum,
queue item 4).

Hypothesis (the funding-arbitrage literature + the F4/G5 forced-flow lineage): the
perp-vs-index premium is arb-anchored — a sharp dislocation of the premium from its own
trailing regime marks one-sided aggressor pressure (leveraged longs paying up, or a
squeeze the other way) that basis-arb desks fade within minutes-to-hours. Template
``premium_dislocation``: at each 1m premium bar end, the z-score of ``premium_close``
against its trailing-24h mean/sigma; when ``|z| >= threshold_sigma``, enter AGAINST the
dislocation (premium rich ⇒ SELL the perp; premium negative-extreme ⇒ BUY) and hold
``hold_minutes``. FW only ever tested funding-CLOCK effects on price bars — the premium
TAPE itself is unmined. Most of the grid is expected to die — the registration is tiny.

Era note (declared, not silent): the premium archive reaches back to 2019-12, but this
registration executes on the 1s tick tape (2023-06→), because the sealed 1m BAR store's
holdout is floor-thinned to days (#144's interval floor) while the tick holdout is ~7
months — a read must match deployable execution on a readable window. The 2020→2023
premium era stays VIRGIN; a slow-premium family on the bar tape would be a NEW
registration.

**MAKER-NATIVE execution** — identical to G6, in ONE tested place
(``maker_fill.run_post_only_fold``, #194-review-hardened; its constants
``LATENCY_S``/``ENTRY_STALE_S``/``TTL_S`` are incorporated by reference): decision-
priced post-only limit, GTX arrival check, strict trade-through within TTL, taker
reduce-only exit. Costs from the one config home: maker fee entry + full taker cost
exit. This IS the deployable execution, so survivors EARN their one-shot holdout read.

Discipline (the sibling shape):

- decisions fire only AT premium bar ends — the 1m close is printed at the decision
  instant (live: the mark-price stream carries the premium in real time); the z-score
  uses trailing samples only (cumsum window ending at the current bar);
- **warmup floor**: no signal until ``_Z_MIN_SAMPLES`` valid 1m samples sit inside the
  trailing window (the holdout fold warms up INSIDE its own window, TEST-3);
- honest-NaN: a non-finite premium bar is no signal; sigma must be strictly positive;
- **non-overlap** and every execution clause: ``maker_fill`` (one position/working
  order at a time; unfinished tail trades DROPPED; exit-minute marks).

TEST-3: ``build_premium_backtesters`` wires the (in-sample, holdout) pair over the
research / holdout TICK stores (prices, fills) AND the research / holdout PREMIUM
stores (the signal input — sealed at the SAME per-series boundaries by
``scripts/seal_premium_store.py``); disjoint roots are asserted on both pairs.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import cast

import numpy as np
from pydantic import BaseModel, ConfigDict

from alpha_core.core.enums import Venue
from alpha_core.data.holdout import assert_disjoint_roots
from alpha_core.data.premium_store import PremiumStore
from alpha_core.data.tick_store import TickStore
from alpha_core.research.cost_scenarios import cost_per_side
from alpha_core.research.depth_backtester import _tick_series, _TickSeries
from alpha_core.research.discovery import Backtester
from alpha_core.research.maker_fill import run_post_only_fold
from alpha_core.research.strategist import DecimalRange, StrategyProposal, StrategyTemplate

# The pre-registered universe: the two premium-deep majors (the arb anchoring the
# premium is thickest there; the F3 verdict already showed the alt tape eats structure).
PREMIUM_CELLS: dict[str, str] = {
    "btcusdt-prem-disloc": "BTCUSDT",
    "ethusdt-prem-disloc": "ETHUSDT",
}
_MINUTE = 60
# The z-score context: trailing 24h of 1m samples (covers all three 8h funding stamps —
# the premium's natural cycle), 12h warmup floor. Chosen a-priori.
_Z_WINDOW = 1440
_Z_MIN_SAMPLES = 720


class PremiumDislocationConfig(BaseModel):
    """Vetted ranges live in ``PREMIUM_TEMPLATES`` — the registration."""

    model_config = ConfigDict(extra="forbid")
    threshold_sigma: Decimal = Decimal("3")
    hold_minutes: int = 30


class _PremiumDislocationSpec:
    def __init__(self, config: PremiumDislocationConfig) -> None:
        if config.threshold_sigma <= 0:
            raise ValueError("threshold_sigma must be positive")
        if not 1 <= config.hold_minutes <= 480:
            raise ValueError("hold_minutes must be in [1, 480]")
        self.config = config


PREMIUM_TEMPLATES: dict[str, StrategyTemplate] = {
    # {3, 4} sigma x {30, 120}m hold = 4 configs per cell, 8 across the 2-cell
    # universe. Widening = a NEW pre-registration.
    "premium_dislocation": StrategyTemplate(
        "premium_dislocation",
        "premium_dislocation",
        PremiumDislocationConfig,
        _PremiumDislocationSpec,
        {
            "threshold_sigma": DecimalRange(Decimal("3"), Decimal("4"), Decimal("1")),
            "hold_minutes": DecimalRange(Decimal("30"), Decimal("120"), Decimal("90")),
        },
    ),
}


def _rolling_mean_sigma(
    x: np.ndarray,
    valid: np.ndarray,
    *,
    window: int = _Z_WINDOW,
    min_samples: int = _Z_MIN_SAMPLES,
) -> tuple[np.ndarray, np.ndarray]:
    """Rolling mean AND std of ``x`` over the trailing ``window`` grid positions, using
    only *valid* samples inside the span (the ``rolling_sigma`` cumsum shape, returning
    the mean too); NaN until ``min_samples`` valid samples sit inside the span."""
    xz = np.where(valid, x, 0.0)
    c1 = np.concatenate(([0.0], np.cumsum(xz)))
    c2 = np.concatenate(([0.0], np.cumsum(xz * xz)))
    cn = np.concatenate(([0], np.cumsum(valid.astype(np.int64))))
    n = len(x)
    lo = np.maximum(np.arange(n + 1) - window, 0)[1:]
    hi = np.arange(1, n + 1)
    nv = cn[hi] - cn[lo]
    s1 = c1[hi] - c1[lo]
    s2 = c2[hi] - c2[lo]
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = s1 / nv
        var = np.maximum(s2 / nv - mean * mean, 0.0)
        sigma = np.sqrt(var)
    thin = nv < min_samples
    mean[thin] = np.nan
    sigma[thin] = np.nan
    return mean, sigma


class PremiumDislocationBacktester:
    """A ``discovery.Backtester`` running the maker-native premium fold on one side."""

    def __init__(self, ticks: TickStore, premium: PremiumStore) -> None:
        self._ticks = ticks
        self._premium = premium
        # maker entry (filled at L, no spread leg) + taker exit: the deployable pair
        self._entry_cost = cost_per_side("maker")
        self._exit_cost = cost_per_side("taker")
        self._px_cache: dict[str, _TickSeries] = {}
        self._pm_cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def _prices(self, symbol: str) -> _TickSeries:
        if symbol not in self._px_cache:
            self._px_cache[symbol] = _tick_series(self._ticks, symbol)
        return self._px_cache[symbol]

    def _premium_series(self, symbol: str) -> tuple[np.ndarray, np.ndarray]:
        if symbol not in self._pm_cache:
            span = self._premium.read_span(venue=Venue.BINANCE, symbol=symbol)
            if len(span.epoch_s) < 2:
                raise ValueError(f"premium fold: no premium bars for {symbol} (run the ingest)")
            self._pm_cache[symbol] = (span.epoch_s, span.premium_close)
        return self._pm_cache[symbol]

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        symbol = PREMIUM_CELLS.get(proposal.window)
        if symbol is None:
            raise ValueError(
                f"unknown premium cell {proposal.window!r}; known: {sorted(PREMIUM_CELLS)}"
            )
        spec = cast(
            _PremiumDislocationSpec, PREMIUM_TEMPLATES[proposal.template].build(proposal.params)
        )
        theta = float(spec.config.threshold_sigma)
        hold = spec.config.hold_minutes * _MINUTE

        ts_p, low, high, close = self._prices(symbol)
        ts_m, prem = self._premium_series(symbol)

        valid = np.isfinite(prem)
        mean, sigma = _rolling_mean_sigma(prem, valid)
        with np.errstate(invalid="ignore", divide="ignore"):
            z = (prem - mean) / sigma
        trigger = valid & np.isfinite(z) & (sigma > 0) & (np.abs(z) >= theta)

        # AGAINST the dislocation: premium rich => SELL the perp, negative => BUY;
        # execution via the shared #184 post-only fold (maker_fill — ONE tested place)
        idx = np.flatnonzero(trigger)
        marks = run_post_only_fold(
            ts_p,
            low,
            high,
            close,
            ts_m[idx].astype(np.float64),
            np.where(z[idx] > 0, -1.0, 1.0),
            hold_s=hold,
            entry_cost=self._entry_cost,
            exit_cost=self._exit_cost,
        )
        return cast(Sequence[float], marks)


def build_premium_backtesters(
    *,
    research_ticks: TickStore,
    holdout_ticks: TickStore,
    research_premium: PremiumStore,
    holdout_premium: PremiumStore,
) -> tuple[Backtester, Backtester]:
    """The TEST-3-critical pair: each side reads ONLY its own tick AND premium stores;
    disjoint roots asserted on both pairs. Execution is fixed maker-native (the
    registration) — there is deliberately no scenario switch."""
    assert_disjoint_roots(research_ticks.root, holdout_ticks.root)
    assert_disjoint_roots(research_premium.root, holdout_premium.root)
    return (
        PremiumDislocationBacktester(research_ticks, research_premium),
        PremiumDislocationBacktester(holdout_ticks, holdout_premium),
    )
