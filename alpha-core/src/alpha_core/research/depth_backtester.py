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
survived 16 bps RT taker; #184 made modelled post-only fills the readable path). The
full rule — decision-priced limit (the #194-review look-ahead fix), GTX arrival check,
strict trade-through within TTL, taker reduce-only exit, busy windows — lives in ONE
tested place, ``maker_fill.run_post_only_fold``, incorporated into this registration by
reference (its constants ``LATENCY_S``/``ENTRY_STALE_S``/``TTL_S`` included). Costs
both sides from the one config home (``cost_scenarios.cost_per_side``): maker fee on
the entry (filled at ``L`` — no spread/slippage leg), full taker cost on the exit. This
asymmetric pair IS the deployable execution, so — unlike fees-only maker scenarios —
survivors here EARN their one-shot holdout read.

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
from alpha_core.research.maker_fill import run_post_only_fold
from alpha_core.research.strategist import DecimalRange, StrategyProposal, StrategyTemplate

# The pre-registered universe: the two depth-published majors (the archive's bookDepth
# tree carries only the liquid tier deep enough for band imbalance to mean anything).
DEPTH_CELLS: dict[str, str] = {
    "btcusdt-depth-imb": "BTCUSDT",
    "ethusdt-depth-imb": "ETHUSDT",
}
_TICK_INTERVAL_S = 1
_MINUTE = 60
# Execution constants (LATENCY_S / ENTRY_STALE_S / TTL_S) live in ``maker_fill`` — the
# shared #184 post-only fold, incorporated into this registration by reference.
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

        # the shared #184 post-only execution fold (maker_fill — ONE tested place):
        # sequential non-overlap, decision-priced limit, GTX, strict trade-through,
        # TTL, taker reduce-only exit, exit-minute marks
        idx = np.flatnonzero(trigger)
        marks = run_post_only_fold(
            ts_p,
            low,
            high,
            close,
            ts_d[idx].astype(np.float64),
            np.where(imb[idx] > 0, 1.0, -1.0),
            hold_s=hold,
            entry_cost=self._entry_cost,
            exit_cost=self._exit_cost,
        )
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
