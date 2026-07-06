"""The G2 taker-flow continuation family — vectorized 1s folds over signed aggressor flow
(`docs/research/intraday-edge-survey-2026-07-v2.md` §G2).

Hypothesis (the microstructure literature's short-horizon flow effect): a burst of one-sided
TAKER volume — the aggressor side of each trade, the only directional intent visible in
public prints — continues over seconds-to-minutes as the initiating meta-order keeps
working and others join. Template ``flow_continuation``: at each price bar, the symbol's
own trailing-``lookback_seconds`` signed imbalance ``(buy - sell) / (buy + sell)``; when
its magnitude clears ``threshold_sigma`` times its trailing sigma, enter WITH the flow and
hold ``hold_seconds``. Most of the grid is expected to die — the registration is tiny.

Discipline (the ``leadlag_backtester`` shape, review-hardened there and mirrored verbatim):

- **2s pre-registered latency** (decision → first executable bar) — a faster-than-latency
  burst earns exactly zero;
- **staleness embargo** on BOTH the flow read and the entry print (a dead tape yields no
  signal and no fill);
- **non-overlap** (one position at a time per cell); unfinished tail trades are DROPPED,
  never fabricated;
- **costs both sides** from the one config home (``cost_scenarios.cost_per_side``):
  ``"taker"`` deployable-today, ``"maker"`` fees-only ⇒ the driver freezes survivors;
- returns land on **exit-minute marks** over the cell's calendar (the OOS slicer sees
  time, not cherry-picked trades).

TEST-3: ``build_flow_backtesters`` wires the (in-sample, holdout) pair over the research /
holdout TICK stores (prices) AND the research / holdout FLOW stores (the signal input —
sealed with the SAME boundaries by ``scripts/seal_flow_store.py``); disjoint roots are
asserted on both pairs.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import cast

import numpy as np
from pydantic import BaseModel, ConfigDict

from alpha_core.core.enums import Venue
from alpha_core.data.flow_store import FlowStore
from alpha_core.data.holdout import assert_disjoint_roots
from alpha_core.data.tick_store import TickStore
from alpha_core.research.cost_scenarios import cost_per_side
from alpha_core.research.discovery import Backtester
from alpha_core.research.leadlag_backtester import _asof, _series, rolling_sigma
from alpha_core.research.strategist import DecimalRange, StrategyProposal, StrategyTemplate

# The pre-registered universe: the two liquid majors only (flow depth matters; alts' flow
# is thin and the F3 verdict already showed the alt tape eats structure).
FLOW_CELLS: dict[str, str] = {
    "btcusdt-1s-flow": "BTCUSDT",
    "ethusdt-1s-flow": "ETHUSDT",
}
_LATENCY_S = 2  # pre-registered execution latency (decision -> first executable bar)
_STALE_S = 3  # max staleness for the flow read and the entry print
_MINUTE = 60


class FlowContinuationConfig(BaseModel):
    """Vetted ranges live in ``FLOW_TEMPLATES`` — the registration."""

    model_config = ConfigDict(extra="forbid")
    lookback_seconds: int = 60
    threshold_sigma: Decimal = Decimal("2")
    hold_seconds: int = 60


class _FlowSpec:
    def __init__(self, config: FlowContinuationConfig) -> None:
        if not 10 <= config.lookback_seconds <= 3600:
            raise ValueError("lookback_seconds must be in [10, 3600]")
        if config.threshold_sigma <= 0:
            raise ValueError("threshold_sigma must be positive")
        if not 10 <= config.hold_seconds <= 3600:
            raise ValueError("hold_seconds must be in [10, 3600]")
        self.config = config


FLOW_TEMPLATES: dict[str, StrategyTemplate] = {
    # {60, 300}s lookback x {2, 3} sigma x {60, 300}s hold = 8 configs per cell, 16 across the
    # 2-cell universe. Widening = a NEW pre-registration.
    "flow_continuation": StrategyTemplate(
        "flow_continuation",
        "flow_continuation",
        FlowContinuationConfig,
        _FlowSpec,
        {
            "lookback_seconds": DecimalRange(Decimal("60"), Decimal("300"), Decimal("240")),
            "threshold_sigma": DecimalRange(Decimal("2"), Decimal("3"), Decimal("1")),
            "hold_seconds": DecimalRange(Decimal("60"), Decimal("300"), Decimal("240")),
        },
    ),
}


class FlowBacktester:
    """A ``discovery.Backtester`` running the flow-continuation fold on one store side."""

    def __init__(self, ticks: TickStore, flows: FlowStore, *, cost_scenario: str = "taker") -> None:
        self._ticks = ticks
        self._flows = flows
        self._cost_side = cost_per_side(cost_scenario)
        self._px_cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._fl_cache: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}

    def _prices(self, symbol: str) -> tuple[np.ndarray, np.ndarray]:
        if symbol not in self._px_cache:
            self._px_cache[symbol] = _series(self._ticks, symbol)
        return self._px_cache[symbol]

    def _flow(self, symbol: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if symbol not in self._fl_cache:
            m = self._flows.read_span(venue=Venue.BINANCE, symbol=symbol, interval_seconds=1)
            if len(m.epoch_s) < 3:
                raise ValueError(f"flow fold: no flow data for {symbol} (run the flow ingest)")
            # cumulative legs; imbalance over (t-L, t] = diff of cumsums at as-of indices
            return self._fl_cache.setdefault(
                symbol, (m.epoch_s, np.cumsum(m.buy), np.cumsum(m.sell))
            )
        return self._fl_cache[symbol]

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        symbol = FLOW_CELLS.get(proposal.window)
        if symbol is None:
            raise ValueError(f"unknown flow cell {proposal.window!r}; known: {sorted(FLOW_CELLS)}")
        spec = cast(_FlowSpec, FLOW_TEMPLATES[proposal.template].build(proposal.params))
        lookback = spec.config.lookback_seconds
        theta = float(spec.config.threshold_sigma)
        hold = spec.config.hold_seconds

        ts_p, px = self._prices(symbol)
        ts_f, cum_buy, cum_sell = self._flow(symbol)

        # trailing-L flow at each PRICE instant (decisions happen on the price tape):
        # as-of cumsum at t minus as-of cumsum at t-L, staleness-guarded at t only (the
        # t-L read may legitimately sit on the last print before a quiet stretch).
        b_now, age_now = _asof(ts_f, cum_buy, ts_p)
        s_now, _ = _asof(ts_f, cum_sell, ts_p)
        b_then, _ = _asof(ts_f, cum_buy, ts_p - lookback)
        s_then, _ = _asof(ts_f, cum_sell, ts_p - lookback)
        buy_l = b_now - b_then
        sell_l = s_now - s_then
        total = buy_l + sell_l
        with np.errstate(invalid="ignore", divide="ignore"):
            imb = (buy_l - sell_l) / total
        valid = (age_now <= _STALE_S) & np.isfinite(imb) & (total > 0)
        sigma = rolling_sigma(np.where(valid, imb, 0.0), valid)
        trigger = valid & np.isfinite(sigma) & (sigma > 0) & (np.abs(imb) >= theta * sigma)

        # sequential non-overlap; entries/exits on the price tape (the leadlag shape)
        n = len(ts_p)
        minutes_lo = int(ts_p[0]) // _MINUTE
        marks = np.zeros(int(ts_p[-1]) // _MINUTE - minutes_lo + 1, dtype=np.float64)
        open_until = -np.inf
        for i in np.flatnonzero(trigger):
            t = float(ts_p[i])
            if t < open_until:
                continue
            entry_idx = int(np.searchsorted(ts_p, t + _LATENCY_S, side="left"))
            if entry_idx >= n:
                break
            if ts_p[entry_idx] - (t + _LATENCY_S) > _STALE_S:
                open_until = float(ts_p[entry_idx])
                continue
            exit_target = float(ts_p[entry_idx]) + hold
            exit_idx = int(np.searchsorted(ts_p, exit_target, side="left"))
            if exit_idx >= n:
                break  # unfinished tail trade: drop, never fabricate
            side = 1.0 if imb[i] > 0 else -1.0
            pnl = side * (px[exit_idx] / px[entry_idx] - 1.0) - 2.0 * self._cost_side
            marks[int(ts_p[exit_idx]) // _MINUTE - minutes_lo] += pnl
            open_until = float(ts_p[exit_idx])
        return cast(Sequence[float], marks)


def build_flow_backtesters(
    *,
    research_ticks: TickStore,
    holdout_ticks: TickStore,
    research_flows: FlowStore,
    holdout_flows: FlowStore,
    cost_scenario: str = "taker",
) -> tuple[Backtester, Backtester]:
    """The TEST-3-critical pair: each side reads ONLY its own tick AND flow stores;
    disjoint roots asserted on both pairs; one scenario prices both sides."""
    assert_disjoint_roots(research_ticks.root, holdout_ticks.root)
    assert_disjoint_roots(research_flows.root, holdout_flows.root)
    return (
        FlowBacktester(research_ticks, research_flows, cost_scenario=cost_scenario),
        FlowBacktester(holdout_ticks, holdout_flows, cost_scenario=cost_scenario),
    )
