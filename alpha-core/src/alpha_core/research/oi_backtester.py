"""The G5 open-interest flush-reversion family — deleveraging snapback on the 1h majors
(`docs/research/intraday-edge-survey-2026-07-v2.md` §G5).

Hypothesis (the F4 liquidation finding's slower cousin, and the strongest prior the round-3
metrics unlock re-opened): a rapid CONTRACTION of open interest is forced deleveraging —
positions closing under duress, not by choice. The accompanying price move overshoots and
partially reverts once the forced flow exhausts. Template ``oi_flush_reversion``: at each 1h
bar close, the trailing-``window_hours`` log-change of open interest (coin-denominated —
USD value would confound with price); when it clears ``threshold_sigma`` trailing sigmas to
the DOWNSIDE, enter AGAINST the concurrent price move (price fell ⇒ long liquidations ⇒
BUY; price rose ⇒ short squeeze ⇒ SELL) and hold ``hold_hours``. Most of the grid is
expected to die — the registration is tiny.

Discipline (the ``leadlag_backtester`` / ``flow_backtester`` shape, review-hardened there):

- **publish embargo** on the metrics tape: the archive stamps each 5-minute snapshot's
  ``create_time``; a decision at bar close ``t`` reads OI as-of ``t - _EMBARGO_S`` (one full
  snapshot period), so no fold ever conditions on a snapshot the live API might not have
  served yet;
- **staleness embargo** on BOTH OI reads (a sparse metrics era yields no signal — the
  archive has real holes) and on the trailing price reference;
- **non-overlap** (one position at a time per cell); unfinished tail trades are DROPPED,
  never fabricated;
- **costs both sides** from the one config home (``cost_scenarios.cost_per_side``):
  ``"taker"`` deployable-today, ``"maker"`` fees-only ⇒ the driver freezes survivors;
- returns land on **exit-hour marks** over the cell's calendar (the OOS slicer sees time,
  not cherry-picked trades). Entry at the decision bar's close — the F2/FW bar-close
  convention on this same 1h plane.

TEST-3: ``build_oi_backtesters`` wires the (in-sample, holdout) pair over the research
``BarStore`` / gate-only ``HoldoutStore`` (prices) AND the research / holdout METRICS
stores (the signal input — sealed at the SAME per-series boundaries by
``scripts/seal_metrics_store.py``); disjoint roots are asserted on both pairs.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import cast

import numpy as np
from pydantic import BaseModel, ConfigDict

from alpha_core.core.enums import Venue
from alpha_core.core.models import Bar
from alpha_core.data.holdout import HoldoutStore, assert_disjoint_roots
from alpha_core.data.metrics_store import MetricsStore
from alpha_core.data.store import BarStore
from alpha_core.research.cost_scenarios import cost_per_side
from alpha_core.research.discovery import Backtester
from alpha_core.research.leadlag_backtester import _asof, rolling_sigma
from alpha_core.research.strategist import DecimalRange, StrategyProposal, StrategyTemplate

# The pre-registered universe: the two metrics-deep majors (the archive's ratio/OI tree is
# only reliable there, and the F3 verdict already showed the alt tape eats structure).
OI_CELLS: dict[str, str] = {
    "btcusdt-1h-oi": "BTCUSDT",
    "ethusdt-1h-oi": "ETHUSDT",
}
_INTERVAL_S = 3600
_HOUR = 3600
_EMBARGO_S = 300  # one 5-min snapshot period: never condition on an unpublished snapshot
_METRIC_STALE_S = 1800  # max as-of staleness for either OI read (sparse era => no signal)
_BAR_STALE_S = 7200  # the trailing price reference may sit one missing bar back, no more
# Sigma over 14 days of hourly signal samples, 7-day warmup floor: chosen a-priori so the
# holdout fold (which warms up INSIDE its own window, TEST-3) keeps most of its era.
_SIGMA_SAMPLES = 336
_SIGMA_MIN_SAMPLES = 168


class OiFlushReversionConfig(BaseModel):
    """Vetted ranges live in ``OI_TEMPLATES`` — the registration."""

    model_config = ConfigDict(extra="forbid")
    window_hours: int = 4
    threshold_sigma: Decimal = Decimal("2")
    hold_hours: int = 4


class _OiFlushSpec:
    def __init__(self, config: OiFlushReversionConfig) -> None:
        if not 1 <= config.window_hours <= 48:
            raise ValueError("window_hours must be in [1, 48]")
        if config.threshold_sigma <= 0:
            raise ValueError("threshold_sigma must be positive")
        if not 1 <= config.hold_hours <= 48:
            raise ValueError("hold_hours must be in [1, 48]")
        self.config = config


OI_TEMPLATES: dict[str, StrategyTemplate] = {
    # {4, 8}h window x {2, 3} sigma x {4, 12}h hold = 8 configs per cell, 16 across the
    # 2-cell universe. Widening = a NEW pre-registration.
    "oi_flush_reversion": StrategyTemplate(
        "oi_flush_reversion",
        "oi_flush",
        OiFlushReversionConfig,
        _OiFlushSpec,
        {
            "window_hours": DecimalRange(Decimal("4"), Decimal("8"), Decimal("4")),
            "threshold_sigma": DecimalRange(Decimal("2"), Decimal("3"), Decimal("1")),
            "hold_hours": DecimalRange(Decimal("4"), Decimal("12"), Decimal("8")),
        },
    ),
}


class OiFlushBacktester:
    """A ``discovery.Backtester`` running the OI-flush fold on one store side."""

    def __init__(
        self,
        bars: Callable[[str], list[Bar]],
        metrics: MetricsStore,
        *,
        cost_scenario: str = "taker",
    ) -> None:
        self._bars = bars
        self._metrics = metrics
        self._cost_side = cost_per_side(cost_scenario)
        self._px_cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._oi_cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def _prices(self, symbol: str) -> tuple[np.ndarray, np.ndarray]:
        """(decision-instant epoch = bar END, close float64) of the cell's 1h series."""
        if symbol not in self._px_cache:
            bars = self._bars(symbol)
            if len(bars) < 3:
                raise ValueError(f"oi fold: no 1h bars for {symbol}")
            dec = np.array([int(b.start.timestamp()) + _INTERVAL_S for b in bars], dtype=np.int64)
            close = np.array([float(b.close) for b in bars], dtype=np.float64)
            self._px_cache[symbol] = (dec, close)
        return self._px_cache[symbol]

    def _oi(self, symbol: str) -> tuple[np.ndarray, np.ndarray]:
        if symbol not in self._oi_cache:
            m = self._metrics.read_span(venue=Venue.BINANCE, symbol=symbol)
            if len(m.epoch_s) < 3:
                raise ValueError(f"oi fold: no metrics for {symbol} (run the metrics ingest)")
            self._oi_cache[symbol] = (m.epoch_s, m.open_interest)
        return self._oi_cache[symbol]

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        symbol = OI_CELLS.get(proposal.window)
        if symbol is None:
            raise ValueError(f"unknown oi cell {proposal.window!r}; known: {sorted(OI_CELLS)}")
        spec = cast(_OiFlushSpec, OI_TEMPLATES[proposal.template].build(proposal.params))
        w = spec.config.window_hours * _HOUR
        theta = float(spec.config.threshold_sigma)
        hold = spec.config.hold_hours * _HOUR

        dec, close = self._prices(symbol)
        ts_m, oi = self._oi(symbol)

        # trailing-W OI log-change as of each bar close, embargoed one snapshot period
        oi_now, age_now = _asof(ts_m, oi, dec - _EMBARGO_S)
        oi_then, age_then = _asof(ts_m, oi, dec - _EMBARGO_S - w)
        with np.errstate(invalid="ignore", divide="ignore"):
            d = np.log(oi_now / oi_then)
        fresh = (
            (age_now <= _METRIC_STALE_S)
            & (age_then <= _METRIC_STALE_S)
            & (oi_now > 0)
            & (oi_then > 0)
            & np.isfinite(d)
        )
        sigma = rolling_sigma(
            np.where(fresh, d, 0.0),
            fresh,
            window=_SIGMA_SAMPLES,
            min_samples=_SIGMA_MIN_SAMPLES,
        )

        # the concurrent price move (same trailing W, on the bar tape) fixes the side
        px_then, age_px = _asof(dec, close, dec - w)
        with np.errstate(invalid="ignore", divide="ignore"):
            pr = close / px_then - 1.0
        directed = (age_px <= _BAR_STALE_S) & np.isfinite(pr) & (pr != 0.0)

        trigger = fresh & directed & np.isfinite(sigma) & (sigma > 0) & (d <= -theta * sigma)

        # sequential non-overlap; entry at the decision bar's close, exit at the first bar
        # close at/after entry + hold (the leadlag shape on an hourly grid)
        n = len(dec)
        hours_lo = int(dec[0]) // _HOUR
        marks = np.zeros(int(dec[-1]) // _HOUR - hours_lo + 1, dtype=np.float64)
        open_until = -np.inf
        for i in np.flatnonzero(trigger):
            t = float(dec[i])
            if t < open_until:
                continue
            exit_idx = int(np.searchsorted(dec, t + hold, side="left"))
            if exit_idx >= n:
                break  # unfinished tail trade: drop, never fabricate
            side = 1.0 if pr[i] < 0 else -1.0  # AGAINST the flush's price move
            pnl = side * (close[exit_idx] / close[i] - 1.0) - 2.0 * self._cost_side
            marks[int(dec[exit_idx]) // _HOUR - hours_lo] += pnl
            open_until = float(dec[exit_idx])
        return cast(Sequence[float], marks)


def build_oi_backtesters(
    *,
    research_store: BarStore,
    holdout_store: HoldoutStore,
    research_metrics: MetricsStore,
    holdout_metrics: MetricsStore,
    cost_scenario: str = "taker",
) -> tuple[Backtester, Backtester]:
    """The TEST-3-critical pair: each side reads ONLY its own bar AND metrics stores;
    disjoint roots asserted on both pairs; one scenario prices both sides."""
    assert_disjoint_roots(research_store.root, holdout_store.root)
    assert_disjoint_roots(research_metrics.root, holdout_metrics.root)

    def _research(symbol: str) -> list[Bar]:
        return research_store.read_bars(
            symbol=symbol, venue=Venue.BINANCE, interval_seconds=_INTERVAL_S
        )

    def _holdout(symbol: str) -> list[Bar]:
        return holdout_store.read_holdout(
            symbol=symbol, venue=Venue.BINANCE, interval_seconds=_INTERVAL_S
        )

    return (
        OiFlushBacktester(_research, research_metrics, cost_scenario=cost_scenario),
        OiFlushBacktester(_holdout, holdout_metrics, cost_scenario=cost_scenario),
    )
