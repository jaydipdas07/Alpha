"""The G6 book-depth imbalance family — MAKER-NATIVE vectorized folds over ±band depth
(`docs/research/intraday-edge-survey-2026-07-v2.md` round-four addendum, queue item 3).

Hypothesis (the microstructure literature's strongest short-horizon signal — Cont/
Kukanov/Stoikov order-flow imbalance; Cartea et al. queue imbalance): when resting
notional within a band of the mark is persistently one-sided, price drifts TOWARD the
thin side's replenishment — i.e. WITH the heavy side — over minutes. Template
``depth_imbalance``: at each ~30 s archive snapshot, the band-``band_pct`` imbalance
``I = (bid - ask) / (bid + ask)``; when ``|I| >= threshold`` on TWO consecutive
snapshots (same sign — the a-priori anti-noise persistence gate), enter WITH the heavy
side and hold ``hold_minutes``. Most of the grid is expected to die — the registration
is tiny.

Bands: the archive publishes ±1..5 % from 2023-01 but the ±0.2 % touch-adjacent rows
only from **2026-01-16** (per-era probe, corrected in #194's review) — AFTER the tick
holdout boundary, so a 0.2 % cell would have zero research-era signal by construction.
The grid therefore registers the ±1 % and ±2 % bands; a 0.2 % family becomes
registrable once its era fattens (a NEW registration).

**MAKER-NATIVE execution — the registration IS the fill rule** (nothing intraday has
survived 16 bps RT taker; #184 made modelled post-only fills the readable path):

- the order is COMPOSED at snapshot time ``t``: the limit ``L`` = the last 1s print
  at/before ``t`` (staleness ≤ ``_ENTRY_STALE_S`` else no order) — ``t``-measurable,
  live-composable; nothing from the flight window may price the order (the #194-review
  look-ahead fix);
- the order arrives at ``t + _LATENCY_S`` (the F3/G2 pre-registered latency) and takes
  the **GTX arrival check** (#184, LTP-strict): an arrival print STRICTLY through ``L``
  ⇒ the venue rejects the post-only order — a missed entry, nothing rests; an at-limit
  arrival print RESTS;
- the resting order fills only on a **strict trade-through**: the first 1s bar strictly
  after arrival, within ``_TTL_S`` (inclusive), whose extreme prints STRICTLY through
  ``L`` (BUY: ``low < L``; SELL: ``high > L``) — a touch never fills (queue position
  unknowable, #184 verbatim). Fill AT ``L``. Unfilled by TTL ⇒ missed entry, never
  chased; the working-order window is busy time (one order at a time);
- the exit is **taker reduce-only** (the #179 contract: a missed entry can never invert
  into an opposite position; guaranteed flat): at the first 1s print at/after
  fill + hold, priced at that print;
- costs both sides from the one config home (``cost_scenarios.cost_per_side``): maker
  fee on the entry (filled at ``L`` — no spread/slippage leg), full taker cost on the
  exit. This asymmetric pair IS the deployable execution, so — unlike fees-only maker
  scenarios — survivors here EARN their one-shot holdout read.

Discipline (the ``leadlag_backtester`` shape, review-hardened there):

- **staleness embargo** on the entry print (a dead tape yields no order); decisions
  happen AT snapshot stamps so the signal read needs no as-of embargo, but persistence
  requires the previous snapshot within ``_PERSIST_MAX_GAP_S`` (the archive has real
  holes — a post-gap snapshot is a fresh burst, not persistence);
- **non-overlap** (one position/working order at a time per cell); unfinished tail
  trades are DROPPED, never fabricated;
- honest-NaN: a snapshot missing either side of the band is no signal;
- returns land on **exit-minute marks** over the cell's calendar (the OOS slicer sees
  time, not cherry-picked trades).

TEST-3: ``build_depth_backtesters`` wires the (in-sample, holdout) pair over the
research / holdout TICK stores (prices, fills) AND the research / holdout DEPTH stores
(the signal input — sealed at the SAME per-series boundaries by
``scripts/seal_depth_store.py``); disjoint roots are asserted on both pairs.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import cast

import numpy as np
import pyarrow.compute as pc
from pydantic import BaseModel, ConfigDict

from alpha_core.core.enums import Venue
from alpha_core.data.depth_store import BANDS, DepthStore
from alpha_core.data.holdout import assert_disjoint_roots
from alpha_core.data.tick_store import TickStore
from alpha_core.research.cost_scenarios import cost_per_side
from alpha_core.research.discovery import Backtester
from alpha_core.research.strategist import DecimalRange, StrategyProposal, StrategyTemplate

# The pre-registered universe: the two depth-published majors (the archive's bookDepth
# tree carries only the liquid tier deep enough for band imbalance to mean anything).
DEPTH_CELLS: dict[str, str] = {
    "btcusdt-depth-imb": "BTCUSDT",
    "ethusdt-depth-imb": "ETHUSDT",
}
_TICK_INTERVAL_S = 1
_MINUTE = 60
_LATENCY_S = 2  # pre-registered execution latency (decision -> order arrival)
_ENTRY_STALE_S = 3  # max staleness of the arrival print that sets the limit L
_TTL_S = 60  # post-only rest window (two snapshot periods); unfilled => missed, never chased
_PERSIST_MAX_GAP_S = 90  # two consecutive snapshots only count within ~3 cadence periods


class DepthImbalanceConfig(BaseModel):
    """Vetted ranges live in ``DEPTH_TEMPLATES`` — the registration."""

    model_config = ConfigDict(extra="forbid")
    band_pct: Decimal = Decimal("1.0")
    threshold: Decimal = Decimal("0.3")
    hold_minutes: int = 15


class _DepthImbalanceSpec:
    def __init__(self, config: DepthImbalanceConfig) -> None:
        if float(config.band_pct) not in BANDS:
            raise ValueError(f"band_pct must be one of {BANDS}")
        if not 0 < config.threshold < 1:
            raise ValueError("threshold must be in (0, 1) — imbalance is a bounded ratio")
        if not 1 <= config.hold_minutes <= 240:
            raise ValueError("hold_minutes must be in [1, 240]")
        self.config = config


DEPTH_TEMPLATES: dict[str, StrategyTemplate] = {
    # {1.0, 2.0}% band x {0.3, 0.5} threshold x {15, 60}m hold = 8 configs per cell, 16
    # across the 2-cell universe. The 0.2% band is NOT registrable (no research-era
    # coverage — see the module docstring). Thresholds are ABSOLUTE (the ratio is
    # already bounded and self-normalized; 0.3 = 2:1 depth, 0.5 = 3:1 — the
    # literature's convention). Widening = a NEW pre-registration.
    "depth_imbalance": StrategyTemplate(
        "depth_imbalance",
        "depth_imbalance",
        DepthImbalanceConfig,
        _DepthImbalanceSpec,
        {
            "band_pct": DecimalRange(Decimal("1.0"), Decimal("2.0"), Decimal("1.0")),
            "threshold": DecimalRange(Decimal("0.3"), Decimal("0.5"), Decimal("0.2")),
            "hold_minutes": DecimalRange(Decimal("15"), Decimal("60"), Decimal("45")),
        },
    ),
}


_TickSeries = tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]


def _tick_series(store: TickStore, symbol: str) -> _TickSeries:
    """(epoch int64, low, high, close float64) of one 1s series, time-ordered."""
    table = store.read_columns(
        venue=Venue.BINANCE,
        symbol=symbol,
        interval_seconds=_TICK_INTERVAL_S,
        columns=("start", "low", "high", "close"),
    )
    if table.num_rows == 0:
        raise ValueError(f"depth fold: no 1s bars for {symbol}")
    ts = np.asarray(
        pc.cast(
            pc.floor(pc.divide(pc.cast(table.column("start"), "int64"), 1_000_000)), "int64"
        ).to_numpy(zero_copy_only=False),
        dtype=np.int64,
    )
    low, high, close = (
        np.asarray(
            pc.cast(table.column(c), "float64").to_numpy(zero_copy_only=False), dtype=np.float64
        )
        for c in ("low", "high", "close")
    )
    return ts, low, high, close


class DepthImbalanceBacktester:
    """A ``discovery.Backtester`` running the maker-native depth fold on one store side."""

    def __init__(self, ticks: TickStore, depth: DepthStore) -> None:
        self._ticks = ticks
        self._depth = depth
        # maker entry (filled at L, no spread leg) + taker exit: the deployable pair
        self._entry_cost = cost_per_side("maker")
        self._exit_cost = cost_per_side("taker")
        self._px_cache: dict[str, _TickSeries] = {}
        self._depth_cache: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}

    def _prices(self, symbol: str) -> _TickSeries:
        if symbol not in self._px_cache:
            self._px_cache[symbol] = _tick_series(self._ticks, symbol)
        return self._px_cache[symbol]

    def _bands(self, symbol: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if symbol not in self._depth_cache:
            span = self._depth.read_span(venue=Venue.BINANCE, symbol=symbol)
            if len(span.epoch_s) < 2:  # persistence needs at least one consecutive pair
                raise ValueError(f"depth fold: no depth snapshots for {symbol} (run the ingest)")
            self._depth_cache[symbol] = (span.epoch_s, span.bid, span.ask)
        return self._depth_cache[symbol]

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        symbol = DEPTH_CELLS.get(proposal.window)
        if symbol is None:
            raise ValueError(
                f"unknown depth cell {proposal.window!r}; known: {sorted(DEPTH_CELLS)}"
            )
        spec = cast(_DepthImbalanceSpec, DEPTH_TEMPLATES[proposal.template].build(proposal.params))
        band_idx = BANDS.index(float(spec.config.band_pct))
        theta = float(spec.config.threshold)
        hold = spec.config.hold_minutes * _MINUTE

        ts_p, low, high, close = self._prices(symbol)
        ts_d, bid_all, ask_all = self._bands(symbol)
        bid = bid_all[:, band_idx]
        ask = ask_all[:, band_idx]

        total = bid + ask
        with np.errstate(invalid="ignore", divide="ignore"):
            imb = (bid - ask) / total
        valid = np.isfinite(imb) & (total > 0)
        armed = valid & (np.abs(imb) >= theta)
        # persistence: this AND the previous snapshot armed, same sign, within the gap
        prev_armed = np.concatenate(([False], armed[:-1]))
        prev_sign = np.concatenate(([0.0], np.sign(imb[:-1])))
        gap_ok = np.concatenate(([False], np.diff(ts_d) <= _PERSIST_MAX_GAP_S))
        trigger = armed & prev_armed & (np.sign(imb) == prev_sign) & gap_ok

        # sequential non-overlap; post-only entry with strict trade-through fill (#184),
        # taker reduce-only exit (the leadlag/flow shape plus a resting-order window)
        n = len(ts_p)
        minutes_lo = int(ts_p[0]) // _MINUTE
        marks = np.zeros(int(ts_p[-1]) // _MINUTE - minutes_lo + 1, dtype=np.float64)
        open_until = -np.inf
        for i in np.flatnonzero(trigger):
            t = float(ts_d[i])
            if t < open_until:
                continue
            # the order is COMPOSED at decision time t: its limit is the last print
            # at/before t — everything in the flight window is unknowable when the
            # order is priced (the #194-review look-ahead fix)
            j0 = int(np.searchsorted(ts_p, t, side="right")) - 1
            if j0 < 0 or t - float(ts_p[j0]) > _ENTRY_STALE_S:
                continue  # dead tape at decision: no order composed
            limit = close[j0]
            side = 1.0 if imb[i] > 0 else -1.0
            arrival = t + _LATENCY_S
            # GTX arrival check (#184 _would_cross_on_arrival, LTP-strict): if the
            # arrival-instant print has traded STRICTLY through the decision-priced
            # limit, the venue rejects the post-only order — missed entry, nothing
            # ever rests (busy only through arrival). An at-limit print RESTS.
            j = int(np.searchsorted(ts_p, arrival, side="right")) - 1
            arrival_ltp = close[j]  # j >= j0 >= 0: the decision print exists
            if (side > 0 and arrival_ltp < limit) or (side < 0 and arrival_ltp > limit):
                open_until = arrival
                continue
            # rest over prints strictly after arrival, within TTL (inclusive — the
            # PaperBroker expires at the first tick PAST valid_until, fills before)
            j_end = int(np.searchsorted(ts_p, arrival + _TTL_S, side="right"))
            if j + 1 >= j_end:
                open_until = arrival + _TTL_S  # no prints inside the window: missed
                continue
            window_ext = low[j + 1 : j_end] if side > 0 else high[j + 1 : j_end]
            through = (window_ext < limit) if side > 0 else (window_ext > limit)
            if not bool(through.any()):
                open_until = arrival + _TTL_S  # TTL expiry: missed entry, never chased
                continue
            m = j + 1 + int(np.argmax(through))
            fill_t = float(ts_p[m])
            exit_idx = int(np.searchsorted(ts_p, fill_t + hold, side="left"))
            if exit_idx >= n:
                break  # unfinished tail trade: drop, never fabricate
            pnl = side * (close[exit_idx] / limit - 1.0) - self._entry_cost - self._exit_cost
            marks[int(ts_p[exit_idx]) // _MINUTE - minutes_lo] += pnl
            open_until = float(ts_p[exit_idx])
        return cast(Sequence[float], marks)


def build_depth_backtesters(
    *,
    research_ticks: TickStore,
    holdout_ticks: TickStore,
    research_depth: DepthStore,
    holdout_depth: DepthStore,
) -> tuple[Backtester, Backtester]:
    """The TEST-3-critical pair: each side reads ONLY its own tick AND depth stores;
    disjoint roots asserted on both pairs. Execution is fixed maker-native (the
    registration) — there is deliberately no scenario switch."""
    assert_disjoint_roots(research_ticks.root, holdout_ticks.root)
    assert_disjoint_roots(research_depth.root, holdout_depth.root)
    return (
        DepthImbalanceBacktester(research_ticks, research_depth),
        DepthImbalanceBacktester(holdout_ticks, holdout_depth),
    )
