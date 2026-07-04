"""The F3 lead-lag family — BTC→alt information diffusion at 1-second resolution
(`docs/research/intraday-edge-survey-2026-07.md` §2.3: tick studies put the mean lead ~57s).

One template, ``leadlag_follow``: when BTC's trailing ``lead_seconds`` return exceeds
``threshold_sigma`` rolling sigmas, take the SAME direction on the (lagging) alt for
``hold_seconds``. The honesty features are the design:

- **Latency is charged, always**: a trigger decided at ``t`` executes at the first alt bar at
  or after ``t + _LATENCY_S`` (2s — Mumbai↔Tokyo RTT plus one engine hop, pre-registered
  constant, not a knob). At this horizon, zero-latency backtests are how people fool
  themselves — the fixture suite pins that a faster-than-latency diffusion earns nothing.
- **Staleness embargo**: the BTC as-of price (signal) must be ≤ ``_STALE_S`` old at decision
  time, and the entry bar must sit within ``_STALE_S`` of the intended execution instant —
  quiet tape means no trade, never a phantom fill at a stale print.
- **Non-overlap**: one open trade per cell; triggers inside an open window are dropped
  (sequential suppression over the sparse trigger set).
- **Taker costs both sides** from ``costs.yaml`` (``crypto_perp`` trading_fee + slippage) —
  the deployable-today execution; a maker variant would be a NEW pre-registration.

Statistics plane: per-trade P&L is attributed to the trade's EXIT minute over a full-span
1-minute grid (zeros elsewhere), so the quant-analyst's OOS slicing sees calendar time. The
lumping of a 1-5 minute trade into one mark raises per-mark variance — conservative for the
Sharpe, never flattering. Floats throughout (never fed back to the money path).

TEST-3: ``build_leadlag_backtesters`` wires the (in-sample, holdout) pair over the research /
holdout **tick** stores; each side reads its own BTC leader series — the holdout fold touches
only holdout months.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import cast

import numpy as np
import pyarrow.compute as pc
from pydantic import BaseModel, ConfigDict

from alpha_core.core.enums import Venue
from alpha_core.data.tick_store import TickStore
from alpha_core.research.discovery import Backtester
from alpha_core.research.funding_window_backtester import taker_cost_per_side
from alpha_core.research.strategist import DecimalRange, StrategyProposal, StrategyTemplate

# The pre-registered universe: BTC is the leader (signal only, never a cell); ETH is excluded
# (co-leader, reserved for the F4 event family); the five liquid laggards are the cells.
LEADLAG_CELLS: dict[str, str] = {
    "solusdt-1s-ll": "SOLUSDT",
    "dogeusdt-1s-ll": "DOGEUSDT",
    "xrpusdt-1s-ll": "XRPUSDT",
    "adausdt-1s-ll": "ADAUSDT",
    "avaxusdt-1s-ll": "AVAXUSDT",
}
LEADER_SYMBOL = "BTCUSDT"
_INTERVAL_S = 1
_LATENCY_S = 2  # pre-registered execution latency (decision -> first executable bar)
_STALE_S = 3  # max as-of staleness for the signal and the entry print
_SIGMA_SAMPLES = 3600  # rolling-sigma window (valid samples on the alt grid)
_SIGMA_MIN_SAMPLES = 600  # warmup floor before any trigger may fire
_MINUTE = 60


class LeadLagFollowConfig(BaseModel):
    """Lead-lag follow tunables (vetted ranges in ``LEADLAG_TEMPLATES``)."""

    model_config = ConfigDict(extra="forbid")
    lead_seconds: int = 60
    threshold_sigma: Decimal = Decimal("2")
    hold_seconds: int = 300


class _FollowSpec:
    def __init__(self, config: LeadLagFollowConfig) -> None:
        if not 1 <= config.lead_seconds <= 600:
            raise ValueError("lead_seconds must be in [1, 600]")
        if config.threshold_sigma <= 0:
            raise ValueError("threshold_sigma must be positive")
        if not 10 <= config.hold_seconds <= 3600:
            raise ValueError("hold_seconds must be in [10, 3600]")
        self.config = config


LEADLAG_TEMPLATES: dict[str, StrategyTemplate] = {
    # {15,60}s lead x {2,4}sigma x {60,300}s hold = 8 configs/cell, 40 across 5 cells.
    "leadlag_follow": StrategyTemplate(
        "leadlag_follow",
        "leadlag_follow",
        LeadLagFollowConfig,
        _FollowSpec,
        {
            "lead_seconds": DecimalRange(Decimal("15"), Decimal("60"), Decimal("45")),
            "threshold_sigma": DecimalRange(Decimal("2"), Decimal("4"), Decimal("2")),
            "hold_seconds": DecimalRange(Decimal("60"), Decimal("300"), Decimal("240")),
        },
    ),
}


def _series(store: TickStore, symbol: str) -> tuple[np.ndarray, np.ndarray]:
    """(epoch-seconds int64, close float64) of one 1s series, time-ordered."""
    table = store.read_columns(
        venue=Venue.BINANCE,
        symbol=symbol,
        interval_seconds=_INTERVAL_S,
        columns=("start", "close"),
    )
    if table.num_rows == 0:
        raise ValueError(f"lead-lag fold: no 1s bars for {symbol}")
    ts = np.asarray(
        pc.cast(
            pc.floor(pc.divide(pc.cast(table.column("start"), "int64"), 1_000_000)), "int64"
        ).to_numpy(zero_copy_only=False),
        dtype=np.int64,
    )
    close = np.asarray(
        pc.cast(table.column("close"), "float64").to_numpy(zero_copy_only=False),
        dtype=np.float64,
    )
    return ts, close


def _asof(ts_ref: np.ndarray, px_ref: np.ndarray, at: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """As-of values of (ts_ref, px_ref) at instants ``at``: (price, age_seconds); age is +inf
    before the first reference print."""
    idx = np.searchsorted(ts_ref, at, side="right") - 1
    ok = idx >= 0
    safe = np.maximum(idx, 0)
    price = px_ref[safe]
    age = np.where(ok, at - ts_ref[safe], np.inf)
    return price, age


def rolling_sigma(
    x: np.ndarray,
    valid: np.ndarray,
    *,
    window: int = _SIGMA_SAMPLES,
    min_samples: int = _SIGMA_MIN_SAMPLES,
) -> np.ndarray:
    """Rolling std of ``x`` over the last ``window`` *valid* samples (invalid entries
    contribute nothing); NaN until ``min_samples`` valid samples have accrued. Shared by the
    tick-scale folds (lead-lag, liquidation)."""
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
    sigma[nv < min_samples] = np.nan
    return np.asarray(sigma, dtype=np.float64)


class LeadLagBacktester:
    """A ``discovery.Backtester`` running the lead-lag fold on one tick-store side."""

    def __init__(self, store: TickStore) -> None:
        self._store = store
        self._cost_side = taker_cost_per_side()
        self._cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def _cached(self, symbol: str) -> tuple[np.ndarray, np.ndarray]:
        if symbol not in self._cache:
            self._cache[symbol] = _series(self._store, symbol)
        return self._cache[symbol]

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        symbol = LEADLAG_CELLS.get(proposal.window)
        if symbol is None:
            raise ValueError(
                f"unknown lead-lag cell {proposal.window!r}; known: {sorted(LEADLAG_CELLS)}"
            )
        spec = cast(_FollowSpec, LEADLAG_TEMPLATES[proposal.template].build(proposal.params))
        k = spec.config.lead_seconds
        theta = float(spec.config.threshold_sigma)
        hold = spec.config.hold_seconds

        ts_b, px_b = self._cached(LEADER_SYMBOL)
        ts_a, px_a = self._cached(symbol)

        # the leader's trailing-k return, as of each alt bar instant
        b_now, age_now = _asof(ts_b, px_b, ts_a)
        b_then, age_then = _asof(ts_b, px_b, ts_a - k)
        with np.errstate(invalid="ignore", divide="ignore"):
            r_k = b_now / b_then - 1.0
        # staleness at BOTH reference instants: a quiet leader tape means no signal
        fresh = (age_now <= _STALE_S) & (age_then <= _STALE_S)
        valid = fresh & np.isfinite(r_k)
        sigma = rolling_sigma(np.where(valid, r_k, 0.0), valid)
        trigger = valid & np.isfinite(sigma) & (sigma > 0) & (np.abs(r_k) >= theta * sigma)

        # sequential non-overlap over the sparse trigger set; entries/exits at as-of alt bars
        n = len(ts_a)
        minutes_lo = int(ts_a[0]) // _MINUTE
        marks = np.zeros(int(ts_a[-1]) // _MINUTE - minutes_lo + 1, dtype=np.float64)
        open_until = -np.inf
        for i in np.flatnonzero(trigger):
            t = float(ts_a[i])
            if t < open_until:
                continue
            entry_idx = int(np.searchsorted(ts_a, t + _LATENCY_S, side="left"))
            if entry_idx >= n:
                break
            # the entry print must be near the intended instant (no fills off a dead tape)
            if ts_a[entry_idx] - (t + _LATENCY_S) > _STALE_S:
                open_until = float(ts_a[entry_idx])  # skip ahead; nothing tradable in the gap
                continue
            exit_target = float(ts_a[entry_idx]) + hold
            exit_idx = int(np.searchsorted(ts_a, exit_target, side="left"))
            if exit_idx >= n:
                break  # unfinished tail trade: drop (no mark) rather than fabricate an exit
            side = 1.0 if r_k[i] > 0 else -1.0
            pnl = side * (px_a[exit_idx] / px_a[entry_idx] - 1.0) - 2.0 * self._cost_side
            marks[int(ts_a[exit_idx]) // _MINUTE - minutes_lo] += pnl
            open_until = float(ts_a[exit_idx])
        return cast(Sequence[float], marks)


def build_leadlag_backtesters(
    *, research_ticks: TickStore, holdout_ticks: TickStore
) -> tuple[Backtester, Backtester]:
    """The TEST-3-critical pair: each side reads ONLY its own tick store (leader included)."""
    return LeadLagBacktester(research_ticks), LeadLagBacktester(holdout_ticks)
