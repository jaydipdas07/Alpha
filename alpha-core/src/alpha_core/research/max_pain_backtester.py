"""The G4 expiry-day max-pain drift family — the NIFTY options-positioning registration
(`docs/research/intraday-edge-survey-2026-07-v2.md` §G4).

Hypothesis (the survey's option-pinning candidate): on expiry day, aggregate option-writer
hedging pressure drags the underlying toward the "max pain" strike — the settlement level
minimizing the total intrinsic payout to option HOLDERS, computed from open interest.
Template ``max_pain_drift``: for each NIFTY expiry session, compute the max-pain strike
``M`` from the PRIOR session's EOD chain (knowable overnight — never the expiry day's own
OI); at the open, the dislocation ``d`` = (decision spot - M) / M; when ``|d| ≥
distance_pct``, enter TOWARD max pain (spot rich ⇒ SHORT, cheap ⇒ LONG) and exit at
``exit_minute`` IST. Most of the grid is expected to die — the registration is tiny.

Fixed protocol constants (part of the registration): decision spot = the last bar
starting inside [09:15, 09:20) IST (the same first-5m info window as G1); entry at the
first bar at/after 09:20, valid only while it starts before 09:30 (staleness, never
interpolation); exit at the first bar at/after ``exit_minute`` (else the session's last
bar — terminal flatten); pre-open prints excluded; IST = UTC+05:30 fixed; max-pain ties
resolve to the LOWEST strike (deterministic).

**Instrument honesty (the F1 overlay precedent):** the fold marks at INDEX levels; the
deployable instrument is the near-month future. Costs both sides from the one config
home (``cost_scenarios.index_future_cost_sides`` — the Budget-2026 futures statute stack
+ 1 bp slippage ≈ 14.7 bps RT); intraday index-vs-future basis drift is unmodelled —
declared here, revisited before any paper deployment.

**Era declared, not silent:** the options stores are sealed at 2024-05-24 (the M5.5
boundary) while the NSE minute-bar stores seal at ≈2026-06-10 (#144's floor). Each fold
side uses only expiries whose prior-day chain lives in ITS OWN options store side, and
its marks calendar is CLIPPED to the chain-covered era (first→last usable expiry): the
research fold therefore runs ≈2016→2024-05 (~400+ weekly/monthly expiries) and the
2024-05→2026-06 stretch is DEAD for this family (bar-research x options-holdout — a
cross-fence read TEST-3 forbids); the holdout fold pairs the options holdout with the
bar holdout (~a handful of expiries until the bar window fattens — the driver defaults
to ``--holdout-reads skip``).

TEST-3: ``build_max_pain_backtesters`` wires the (in-sample, holdout) pair over the
research/holdout BAR stores AND the research/holdout OPTIONS stores; disjoint roots are
asserted on both pairs; neither side can read across a fence.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import NamedTuple, cast

import numpy as np
from pydantic import BaseModel, ConfigDict

from alpha_core.core.enums import Venue
from alpha_core.core.models import Bar
from alpha_core.data.holdout import HoldoutStore, assert_disjoint_roots
from alpha_core.data.options_store import OptionsStore
from alpha_core.data.store import BarStore
from alpha_core.research.cost_scenarios import index_future_cost_sides
from alpha_core.research.discovery import Backtester
from alpha_core.research.sip_orb_backtester import _IST, _OPEN_MIN, BarReader
from alpha_core.research.strategist import DecimalRange, StrategyProposal, StrategyTemplate

# The single pre-registered cell: the NIFTY index minute series (the value IS the bar
# symbol, the OI_CELLS convention) + the NIFTY option chains.
MAX_PAIN_CELLS: dict[str, str] = {"nifty-max-pain": "NSE:NIFTY 50"}
_UNDERLYING = "NIFTY"
_INTERVAL_S = 60
_ORB_END_MIN = 9 * 60 + 20  # decision spot = last bar starting inside [09:15, 09:20)
_ENTRY_STALE_MIN = 9 * 60 + 30  # the entry bar must start before 09:30 IST


class MaxPainDriftConfig(BaseModel):
    """Vetted ranges live in ``MAX_PAIN_TEMPLATES`` — the registration."""

    model_config = ConfigDict(extra="forbid")
    distance_pct: Decimal = Decimal("0.3")
    exit_minute: int = 870  # IST minutes-of-day: 870 = 14:30, 915 = 15:15


class _MaxPainSpec:
    def __init__(self, config: MaxPainDriftConfig) -> None:
        if not 0 < config.distance_pct <= 5:
            raise ValueError("distance_pct must be in (0, 5] (percent)")
        if not 10 * 60 <= config.exit_minute <= 15 * 60 + 20:
            raise ValueError("exit_minute must be in [600, 920] IST minutes")
        self.config = config


MAX_PAIN_TEMPLATES: dict[str, StrategyTemplate] = {
    # {0.3, 0.6}% distance x {14:30, 15:15} exit = 4 configs, ONE cell = 4 trials.
    # Widening = a NEW pre-registration.
    "max_pain_drift": StrategyTemplate(
        "max_pain_drift",
        "max_pain_drift",
        MaxPainDriftConfig,
        _MaxPainSpec,
        {
            "distance_pct": DecimalRange(Decimal("0.3"), Decimal("0.6"), Decimal("0.3")),
            "exit_minute": DecimalRange(Decimal("870"), Decimal("915"), Decimal("45")),
        },
    ),
}


class _Day(NamedTuple):
    """One index session, pre-digested (floats: the statistics plane)."""

    decision_spot: float  # close of the last bar starting inside [09:15, 09:20) IST
    minutes: np.ndarray  # IST minutes-of-day of POST-window bar starts, int64
    close: np.ndarray


def _digest_day(bars: Sequence[Bar]) -> _Day | None:
    decision: float | None = None
    minutes: list[int] = []
    close: list[float] = []
    for b in bars:
        ist = b.start + _IST
        m = ist.hour * 60 + ist.minute
        if m < _OPEN_MIN:
            continue  # pre-open prints: not the session tape
        c = float(b.close)
        if m < _ORB_END_MIN:
            decision = c  # the LAST bar inside the window wins (time-ordered reader)
            continue
        minutes.append(m)
        close.append(c)
    if decision is None or decision <= 0 or not minutes:
        return None
    return _Day(decision, np.array(minutes, dtype=np.int64), np.array(close, dtype=np.float64))


def max_pain_strike(strikes: np.ndarray, is_call: np.ndarray, oi: np.ndarray) -> float:
    """The settlement level (from the strike grid) minimizing total intrinsic payout to
    holders: argmin_S  Σ OI_call(K)*max(S-K,0) + OI_put(K)*max(K-S,0); ties resolve to
    the LOWEST strike (deterministic). Pure — unit-tested directly."""
    grid = np.unique(strikes)
    call_k = strikes[is_call]
    call_oi = oi[is_call]
    put_k = strikes[~is_call]
    put_oi = oi[~is_call]
    pain = np.array(
        [
            float((call_oi * np.maximum(s - call_k, 0.0)).sum())
            + float((put_oi * np.maximum(put_k - s, 0.0)).sum())
            for s in grid
        ]
    )
    return float(grid[int(np.argmin(pain))])  # argmin takes the first (lowest) on ties


def _prior_day_max_pain(store: OptionsStore) -> dict[date, float]:
    """expiry session -> max-pain strike from the PRIOR session's chain for THAT expiry.

    One DuckDB pass over the store side (arrow-native — no per-row pydantic); for each
    expiry the prior session = the latest trade_date STRICTLY BEFORE the expiry day
    among that expiry's own rows (holidays handled naturally). An expiry with no prior
    chain is absent (no trade)."""
    con = store.connect()
    table = con.execute(
        "SELECT CAST(trade_date AS DATE) AS td, CAST(expiry AS DATE) AS ed, "
        "CAST(strike AS DOUBLE) AS k, \"right\" = 'CALL' AS is_call, "
        "CAST(open_interest AS DOUBLE) AS oi "
        "FROM option_quotes WHERE underlying = ? AND venue = ?",
        [_UNDERLYING, Venue.NSE.value],
    ).fetch_arrow_table()
    con.close()
    if table.num_rows == 0:
        return {}
    td = table.column("td").to_pylist()
    ed = table.column("ed").to_pylist()
    k = np.asarray(table.column("k").to_numpy(zero_copy_only=False), dtype=np.float64)
    is_call = np.asarray(table.column("is_call").to_numpy(zero_copy_only=False), dtype=bool)
    oi = np.asarray(table.column("oi").to_numpy(zero_copy_only=False), dtype=np.float64)

    by_expiry: dict[date, dict[date, list[int]]] = {}
    for i, (t, e) in enumerate(zip(td, ed, strict=True)):
        by_expiry.setdefault(e, {}).setdefault(t, []).append(i)
    out: dict[date, float] = {}
    for e, days in by_expiry.items():
        prior_days = [t for t in days if t < e]
        if not prior_days:
            continue  # no prior chain: the expiry is untradeable for this family
        idx = np.array(days[max(prior_days)], dtype=np.int64)
        out[e] = max_pain_strike(k[idx], is_call[idx], oi[idx])
    return out


class MaxPainBacktester:
    """A ``discovery.Backtester`` running the max-pain drift fold on one store-side pair."""

    def __init__(self, reader: BarReader, options: OptionsStore) -> None:
        self._reader = reader
        self._options = options
        self._buy_cost, self._sell_cost = index_future_cost_sides()
        self._days_cache: dict[date, _Day] | None = None
        self._pain_cache: dict[date, float] | None = None

    def _days(self, symbol: str) -> dict[date, _Day]:
        if self._days_cache is None:
            bars = self._reader(symbol)
            by_day: dict[date, list[Bar]] = {}
            for b in bars:
                by_day.setdefault((b.start + _IST).date(), []).append(b)
            digests: dict[date, _Day] = {}
            for d, day_bars in sorted(by_day.items()):
                digest = _digest_day(day_bars)
                if digest is not None:
                    digests[d] = digest
            if not digests:
                raise ValueError(f"max-pain fold: no minute bars for {symbol} (run the ingest)")
            self._days_cache = digests
        return self._days_cache

    def _pain(self) -> dict[date, float]:
        if self._pain_cache is None:
            pain = _prior_day_max_pain(self._options)
            if not pain:
                raise ValueError("max-pain fold: no usable NIFTY chains (run the options ingest)")
            self._pain_cache = pain
        return self._pain_cache

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        symbol = MAX_PAIN_CELLS.get(proposal.window)
        if symbol is None:
            raise ValueError(
                f"unknown max-pain cell {proposal.window!r}; known: {sorted(MAX_PAIN_CELLS)}"
            )
        spec = cast(_MaxPainSpec, MAX_PAIN_TEMPLATES[proposal.template].build(proposal.params))
        theta = float(spec.config.distance_pct) / 100.0
        exit_minute = spec.config.exit_minute

        days = self._days(symbol)
        pain = self._pain()
        # the marks calendar: bar sessions CLIPPED to the chain-covered era (see the
        # module docstring — a fence gap must not dilute the marks with dead zeros)
        lo, hi = min(pain), max(pain)
        sessions = [d for d in sorted(days) if lo <= d <= hi]
        if not sessions:
            raise ValueError("max-pain fold: bar sessions and chain era do not overlap")
        marks = np.zeros(len(sessions), dtype=np.float64)
        for i, d in enumerate(sessions):
            m = pain.get(d)
            if m is None or m <= 0:
                continue  # not an expiry session (or no prior chain): no trade
            day = days[d]
            dislocation = (day.decision_spot - m) / m
            if abs(dislocation) < theta:
                continue
            side = -1.0 if dislocation > 0 else 1.0  # TOWARD max pain
            n = len(day.minutes)
            entry_idx = 0  # post-window arrays start at the first bar >= 09:20
            if day.minutes[entry_idx] >= _ENTRY_STALE_MIN:
                continue  # stale open tape: no trade
            exit_idx = min(int(np.searchsorted(day.minutes, exit_minute, side="left")), n - 1)
            if entry_idx > exit_idx:
                continue
            entry = day.close[entry_idx]
            if entry <= 0:
                continue  # data artifact; never divide by it
            gross = side * (day.close[exit_idx] / entry - 1.0)
            marks[i] = gross - self._buy_cost - self._sell_cost
        return cast(Sequence[float], marks)


def build_max_pain_backtesters(
    *,
    research_store: BarStore,
    holdout_store: HoldoutStore,
    research_options: OptionsStore,
    holdout_options: OptionsStore,
) -> tuple[Backtester, Backtester]:
    """The TEST-3-critical pair: each side reads ONLY its own bar AND options stores;
    disjoint roots asserted on both pairs — no cross-fence read is constructible."""
    assert_disjoint_roots(research_store.root, holdout_store.root)
    assert_disjoint_roots(research_options.root, holdout_options.root)

    def _research(symbol: str) -> Sequence[Bar]:
        return research_store.read_bars(
            symbol=symbol, venue=Venue.NSE, interval_seconds=_INTERVAL_S
        )

    def _holdout(symbol: str) -> Sequence[Bar]:
        return holdout_store.read_holdout(
            symbol=symbol, venue=Venue.NSE, interval_seconds=_INTERVAL_S
        )

    return (
        MaxPainBacktester(_research, research_options),
        MaxPainBacktester(_holdout, holdout_options),
    )
