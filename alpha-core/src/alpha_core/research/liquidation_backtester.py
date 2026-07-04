"""The F4 liquidation-cascade reversion fold — EXPLORATORY on the archived era
(`docs/research/intraday-edge-survey-2026-07.md` §2.5).

Hypothesis: clustered forced liquidations overshoot price in the forcing direction, then
revert. Signal: net forced flow over a trailing ``window_seconds`` — BUY liquidations
(shorts force-closed, upward pressure) minus SELL liquidations — as a z-score against its
rolling distribution; trade AGAINST the cascade for ``hold_seconds``, latency-charged and
non-overlapping (the lead-lag trade mechanics, shared helpers).

**Why this fold is exploratory-only, never a gate:** the only archived liquidation history
Binance publishes is coin-margined and ENDS 2024-10-14 — before any sensible holdout window
begins, so an archive-era "holdout read" is impossible by construction; and the deployable
live signal is the um forceOrder stream (downsampled by Binance to ≤1 order/s/symbol), a
DIFFERENT source. The gate-eligible F4 family is pre-registered to be judged on recorder-era
um data (``scripts/record_liquidations.py``) once months accumulate. This fold's job is the
cheap prior: does archive-era cascade reversion clear costs in-sample at all?

Events are conditioning data (the funding-store precedent): unsealed, read whole; the
tradeable bars come from the research tick store only.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import cast

import numpy as np
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict

from alpha_core.data.tick_store import TickStore
from alpha_core.research.discovery import Backtester
from alpha_core.research.funding_window_backtester import taker_cost_per_side
from alpha_core.research.leadlag_backtester import _series, rolling_sigma
from alpha_core.research.strategist import DecimalRange, StrategyProposal, StrategyTemplate

# cell -> (um bar symbol, cm event contract). BTC + ETH: the two contracts the archive covers.
LIQ_CELLS: dict[str, tuple[str, str]] = {
    "btcusdt-1s-lr": ("BTCUSDT", "BTCUSD_PERP"),
    "ethusdt-1s-lr": ("ETHUSDT", "ETHUSD_PERP"),
}
_LATENCY_S = 2
_STALE_S = 3
_Z_WINDOW = 86_400  # rolling-z window: one day of 1s samples
_Z_MIN_SAMPLES = 14_400  # 4h warmup
_MINUTE = 60


class LiqRevertConfig(BaseModel):
    """Cascade-reversion tunables (vetted ranges in ``LIQ_TEMPLATES``)."""

    model_config = ConfigDict(extra="forbid")
    window_seconds: int = 300
    z_min: Decimal = Decimal("4")
    hold_seconds: int = 300


class _RevertSpec:
    def __init__(self, config: LiqRevertConfig) -> None:
        if not 10 <= config.window_seconds <= 3600:
            raise ValueError("window_seconds must be in [10, 3600]")
        if config.z_min <= 0:
            raise ValueError("z_min must be positive")
        if not 10 <= config.hold_seconds <= 7200:
            raise ValueError("hold_seconds must be in [10, 7200]")
        self.config = config


LIQ_TEMPLATES: dict[str, StrategyTemplate] = {
    # {60,300}s window x {4,8}z x {300,900}s hold = 8 configs/cell, 16 across both cells.
    "liq_cascade_revert": StrategyTemplate(
        "liq_cascade_revert",
        "liq_cascade_revert",
        LiqRevertConfig,
        _RevertSpec,
        {
            "window_seconds": DecimalRange(Decimal("60"), Decimal("300"), Decimal("240")),
            "z_min": DecimalRange(Decimal("4"), Decimal("8"), Decimal("4")),
            "hold_seconds": DecimalRange(Decimal("300"), Decimal("900"), Decimal("600")),
        },
    ),
}


def load_liq_events(liq_root: Path, contract: str) -> tuple[np.ndarray, np.ndarray]:
    """(epoch-seconds int64, signed notional float64) — BUY (shorts squeezed, upward push)
    positive, SELL negative; time-ordered."""
    table = pq.read_table(liq_root / f"{contract}.parquet")
    if table.num_rows == 0:
        raise ValueError(f"no liquidation events for {contract}")
    ts = np.array([int(t.timestamp()) for t in table.column("ts").to_pylist()], dtype=np.int64)
    qty = np.asarray(table.column("quantity").to_numpy(zero_copy_only=False), dtype=np.float64)
    px = np.asarray(table.column("price").to_numpy(zero_copy_only=False), dtype=np.float64)
    side = np.array([1.0 if s == "BUY" else -1.0 for s in table.column("side").to_pylist()])
    order = np.argsort(ts, kind="stable")
    return ts[order], (side * qty * px)[order]


class LiquidationBacktester:
    """A ``discovery.Backtester`` running the cascade-reversion fold (research side only —
    the archive era supports no holdout, see module docstring)."""

    def __init__(self, ticks: TickStore, liq_root: Path) -> None:
        self._ticks = ticks
        self._liq_root = liq_root
        self._cost_side = taker_cost_per_side()
        self._bar_cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._event_cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        pair = LIQ_CELLS.get(proposal.window)
        if pair is None:
            raise ValueError(
                f"unknown liquidation cell {proposal.window!r}; known: {sorted(LIQ_CELLS)}"
            )
        bar_symbol, contract = pair
        spec = cast(_RevertSpec, LIQ_TEMPLATES[proposal.template].build(proposal.params))
        w = spec.config.window_seconds
        z_min = float(spec.config.z_min)
        hold = spec.config.hold_seconds

        if bar_symbol not in self._bar_cache:
            self._bar_cache[bar_symbol] = _series(self._ticks, bar_symbol)
        ts_a, px_a = self._bar_cache[bar_symbol]
        if contract not in self._event_cache:
            self._event_cache[contract] = load_liq_events(self._liq_root, contract)
        ts_e, flow = self._event_cache[contract]

        # net forced flow over (t-w, t]: cumulative-flow as-of t minus as-of t-w
        cum = np.concatenate(([0.0], np.cumsum(flow)))
        idx_now = np.searchsorted(ts_e, ts_a, side="right")
        idx_then = np.searchsorted(ts_e, ts_a - w, side="right")
        f = cum[idx_now] - cum[idx_then]
        valid = np.ones_like(f, dtype=bool)
        sigma = rolling_sigma(f, valid, window=_Z_WINDOW, min_samples=_Z_MIN_SAMPLES)
        with np.errstate(invalid="ignore", divide="ignore"):
            z = f / sigma
        trigger = np.isfinite(z) & (np.abs(z) >= z_min) & (sigma > 0)

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
            if ts_a[entry_idx] - (t + _LATENCY_S) > _STALE_S:
                open_until = float(ts_a[entry_idx])
                continue
            exit_idx = int(np.searchsorted(ts_a, float(ts_a[entry_idx]) + hold, side="left"))
            if exit_idx >= n:
                break
            side = -1.0 if z[i] > 0 else 1.0  # fade the cascade's push
            pnl = side * (px_a[exit_idx] / px_a[entry_idx] - 1.0) - 2.0 * self._cost_side
            marks[int(ts_a[exit_idx]) // _MINUTE - minutes_lo] += pnl
            open_until = float(ts_a[exit_idx])
        return cast(Sequence[float], marks)


def build_liquidation_explorer(*, research_ticks: TickStore, liq_root: Path) -> Backtester:
    """Research-side fold ONLY — there is deliberately no holdout twin (module docstring)."""
    return LiquidationBacktester(research_ticks, liq_root)
