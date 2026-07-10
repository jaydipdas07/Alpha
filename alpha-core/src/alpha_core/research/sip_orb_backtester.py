"""The G1 Stocks-in-Play 5-minute opening-range-breakout family — the PORTFOLIO-path
registration on the NIFTY-100 minute panel (`docs/research/intraday-edge-survey-2026-07-v2.md`
§G1 — THE strongest external evidence on the board: Zarattini/Barbon/Aziz replicate
Sharpe ≈ 2.4 on the US analogue).

Hypothesis: stocks "in play" — abnormal opening volume (RVOL) — carry intraday momentum;
the first 5-minute candle's range defines the day's noise floor and its DIRECTION picks
the side; a breakout of that range in the first-candle direction, trailed on VWAP and
squared off before the close, harvests the drift. Template ``sip_orb``: each session,
rank the universe by RVOL (first-5m volume over its trailing-14-session median); take the
``top_k`` names with RVOL ≥ ``rvol_threshold`` whose first candle has a body; enter WITH
the first-candle direction when a later 1-minute close breaks the opening range on that
side; trail on the running day-VWAP; square off 15:25 IST. Most of the grid is expected
to die — the registration is tiny.

**The PORTFOLIO path** (the pre-registered design — per-stock cells would scatter
multiplicity): ONE panel cell, one ledger row per config; each session allocates
``1/top_k`` capital to each selected name (unfilled slots stay flat — reserved capital
earns 0, honestly); the fold returns ONE mark per session (the OOS slicer sees the
trading calendar, not cherry-picked trades).

Fixed protocol constants (part of the registration — a knob here would be a new
hypothesis): 5-minute opening range (bars starting 09:15-09:19 IST; earlier pre-open
prints are excluded from the range, the VWAP, and the tape); RVOL lookback 14 sessions
(median, all 14 required — warmup happens INSIDE each store side, TEST-3); entries only
before 15:00 IST; **one-bar deferral** on entry AND exit (a 1-minute family must not
inherit the 1h zero-deferral convention — a signal on bar ``t``'s close executes at bar
``t+1``'s close, taker); VWAP = cumulative close x volume from the session open; hard
square-off at the first bar at/after 15:25 IST (a session with no such bar exits at its
last bar — the engine's terminal-flatten convention); IST = UTC+05:30 fixed (NSE has no
DST).

Costs both sides from the one config home (``cost_scenarios.equity_intraday_cost_sides``):
the full Indian MIS cash-equity stack — brokerage pct + exchange txn + SEBI + GST (the
#177 fix) + buy-side stamp / sell-side STT + 5 bps slippage per side ≈ 20.6 bps RT.
Deployable-today execution ⇒ survivors earn their one-shot read — BUT see the driver:
the NSE|60 holdout window is floor-thinned (#144) to ~20 sessions minus the 14-session
RVOL warmup, so the driver defaults to ``--holdout-reads skip`` (freeze survivors; read
when the window fattens) — the #169/#179 precedent.

**Survivorship DECLARED, not hidden:** the universe is today's NIFTY-100 core
(`docs/research/nifty100-universe-2026-07.txt`), not point-in-time membership. In-play
selection conditions on the day's own opening volume, which mutes (but does not remove)
index-inclusion bias; treat any survivor as evidence-to-refine, not a certificate.

TEST-3: ``build_sip_orb_backtesters`` wires the (in-sample, holdout) pair over the
research ``BarStore`` / gate-only ``HoldoutStore``; disjoint roots asserted; each side
reads ONLY its own store and warms its RVOL window inside its own era.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, timedelta
from decimal import Decimal
from statistics import median
from typing import NamedTuple, cast

import numpy as np
from pydantic import BaseModel, ConfigDict

from alpha_core.core.enums import Venue
from alpha_core.core.models import Bar
from alpha_core.data.holdout import HoldoutStore, assert_disjoint_roots
from alpha_core.data.store import BarStore
from alpha_core.research.cost_scenarios import equity_intraday_cost_sides
from alpha_core.research.discovery import Backtester
from alpha_core.research.strategist import DecimalRange, StrategyProposal, StrategyTemplate

# The single pre-registered panel cell (per-stock cells would scatter multiplicity).
SIP_ORB_CELLS: dict[str, str] = {"nse-sip-orb": "NIFTY100-2026-07"}
_INTERVAL_S = 60
_IST = timedelta(hours=5, minutes=30)  # NSE wall clock; no DST
_OPEN_MIN = 9 * 60 + 15  # 09:15 IST — the first ORB bar; earlier prints are pre-open
_ORB_END_MIN = 9 * 60 + 20  # opening range = bars starting in [09:15, 09:20) IST
_ENTRY_CUTOFF_MIN = 15 * 60  # no fresh entries at/after 15:00 IST
_SQUARE_OFF_MIN = 15 * 60 + 25  # hard flat at the first bar at/after 15:25 IST
_RVOL_LOOKBACK = 14  # sessions in the trailing first-5m volume median (all required)

# A per-symbol bar reader — the injected store boundary (research or holdout side).
BarReader = Callable[[str], Sequence[Bar]]


class SipOrbConfig(BaseModel):
    """Vetted ranges live in ``SIP_ORB_TEMPLATES`` — the registration."""

    model_config = ConfigDict(extra="forbid")
    rvol_threshold: Decimal = Decimal("2")
    top_k: int = 5


class _SipOrbSpec:
    def __init__(self, config: SipOrbConfig) -> None:
        if not 1 < config.rvol_threshold <= 10:
            raise ValueError("rvol_threshold must be in (1, 10]")
        if not 2 <= config.top_k <= 20:
            raise ValueError("top_k must be in [2, 20]")
        self.config = config


SIP_ORB_TEMPLATES: dict[str, StrategyTemplate] = {
    # {2, 3} RVOL x {5, 10} top_k = 4 configs, ONE panel cell = 4 trials total.
    # Widening = a NEW pre-registration.
    "sip_orb": StrategyTemplate(
        "sip_orb",
        "sip_orb",
        SipOrbConfig,
        _SipOrbSpec,
        {
            "rvol_threshold": DecimalRange(Decimal("2"), Decimal("3"), Decimal("1")),
            "top_k": DecimalRange(Decimal("5"), Decimal("10"), Decimal("5")),
        },
    ),
}


class _Day(NamedTuple):
    """One symbol-session, pre-digested for the fold (floats: the statistics plane)."""

    orb_high: float
    orb_low: float
    direction: float  # sign(first-candle body); 0.0 = doji, never trades
    first5_volume: float
    # post-ORB session bars (start in [09:20, ...) IST): parallel arrays, time-ordered
    minutes: np.ndarray  # IST minutes-of-day of bar STARTS, int64
    close: np.ndarray
    vwap: np.ndarray  # running day-VWAP (close x volume, cumulative from 09:15)


def _digest_day(bars: Sequence[Bar]) -> _Day | None:
    """Digest one symbol-session; None when the ORB window is absent/empty."""
    orb_high = -np.inf
    orb_low = np.inf
    first_open: float | None = None
    orb_close = 0.0
    first5_volume = 0.0
    cum_pv = 0.0
    cum_v = 0.0
    minutes: list[int] = []
    close: list[float] = []
    vwap: list[float] = []
    for b in bars:
        ist = b.start + _IST
        m = ist.hour * 60 + ist.minute
        if m < _OPEN_MIN:
            continue  # pre-open auction prints: not the session tape
        c = float(b.close)
        v = float(b.volume)
        cum_pv += c * v
        cum_v += v
        if m < _ORB_END_MIN:
            if first_open is None:
                first_open = float(b.open)
            orb_high = max(orb_high, float(b.high))
            orb_low = min(orb_low, float(b.low))
            orb_close = c
            first5_volume += v
            continue
        minutes.append(m)
        close.append(c)
        vwap.append(cum_pv / cum_v if cum_v > 0 else c)
    if first_open is None or not minutes:
        return None  # no opening range (halted open / missing tape) or no post-ORB bars
    return _Day(
        orb_high,
        orb_low,
        float(np.sign(orb_close - first_open)),
        first5_volume,
        np.array(minutes, dtype=np.int64),
        np.array(close, dtype=np.float64),
        np.array(vwap, dtype=np.float64),
    )


def _member_return(day: _Day, side: float, buy_cost: float, sell_cost: float) -> float:
    """One selected member's session return under the registered execution: one-bar
    deferred breakout entry, one-bar deferred VWAP-trail exit, hard square-off."""
    n = len(day.minutes)
    sq_idx = int(np.searchsorted(day.minutes, _SQUARE_OFF_MIN, side="left"))
    last_idx = min(sq_idx, n - 1)  # no 15:25 bar => the session's last bar (terminal flatten)
    trigger = (day.close > day.orb_high) if side > 0 else (day.close < day.orb_low)
    trigger &= day.minutes < _ENTRY_CUTOFF_MIN
    t_idx = int(np.argmax(trigger)) if bool(trigger.any()) else -1
    if t_idx < 0 or t_idx + 1 > last_idx:
        return 0.0  # never triggered before cutoff, or no bar left to enter on
    entry_idx = t_idx + 1  # one-bar deferral
    entry = day.close[entry_idx]
    if entry <= 0:
        return 0.0  # data artifact; never divide by it
    # VWAP trail after entry: first bar whose close crosses the running VWAP against us
    post = slice(entry_idx + 1, last_idx + 1)
    crossed = (day.close[post] < day.vwap[post]) if side > 0 else (day.close[post] > day.vwap[post])
    if bool(crossed.any()):
        exit_idx = min(entry_idx + 1 + int(np.argmax(crossed)) + 1, last_idx)  # deferred, capped
    else:
        exit_idx = last_idx
    gross = side * (day.close[exit_idx] / entry - 1.0)
    return float(gross - buy_cost - sell_cost)


class SipOrbBacktester:
    """A ``discovery.Backtester`` running the SIP-ORB portfolio fold on one store side."""

    def __init__(self, reader: BarReader, universe: Sequence[str]) -> None:
        self._reader = reader
        self._universe = list(universe)
        self._buy_cost, self._sell_cost = equity_intraday_cost_sides()
        self._digest_cache: dict[str, dict[date, _Day]] | None = None

    def _load(self) -> dict[str, dict[date, _Day]]:
        if self._digest_cache is not None:
            return self._digest_cache
        digests: dict[str, dict[date, _Day]] = {}
        for symbol in self._universe:
            bars = self._reader(symbol)
            if not bars:
                continue  # un-ingested member: out of the cross-section (declared)
            by_day: dict[date, list[Bar]] = {}
            for b in bars:
                by_day.setdefault((b.start + _IST).date(), []).append(b)
            day_digests: dict[date, _Day] = {}
            for d, day_bars in sorted(by_day.items()):
                digest = _digest_day(day_bars)
                if digest is not None:
                    day_digests[d] = digest
            if day_digests:
                digests[symbol] = day_digests
        if not digests:
            raise ValueError(
                "sip-orb fold: no minute bars for any universe member (run the ingest)"
            )
        self._digest_cache = digests
        return digests

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        if proposal.window not in SIP_ORB_CELLS:
            raise ValueError(
                f"unknown sip-orb cell {proposal.window!r}; known: {sorted(SIP_ORB_CELLS)}"
            )
        spec = cast(_SipOrbSpec, SIP_ORB_TEMPLATES[proposal.template].build(proposal.params))
        theta = float(spec.config.rvol_threshold)
        top_k = spec.config.top_k

        digests = self._load()
        sessions = sorted({d for days in digests.values() for d in days})
        marks = np.zeros(len(sessions), dtype=np.float64)
        history: dict[str, list[float]] = {s: [] for s in digests}
        for i, d in enumerate(sessions):
            candidates: list[tuple[float, str, _Day]] = []
            for symbol, days in digests.items():
                day = days.get(d)
                if day is None:
                    continue
                hist = history[symbol]
                if len(hist) >= _RVOL_LOOKBACK and day.direction != 0.0:
                    med = median(hist[-_RVOL_LOOKBACK:])
                    if med > 0 and day.first5_volume / med >= theta:
                        candidates.append((day.first5_volume / med, symbol, day))
                hist.append(day.first5_volume)  # the day's own volume joins AFTER its check
            if candidates:
                candidates.sort(key=lambda c: (-c[0], c[1]))  # deterministic: RVOL desc, symbol
                day_pnl = sum(
                    _member_return(day, day.direction, self._buy_cost, self._sell_cost)
                    for _, _, day in candidates[:top_k]
                )
                marks[i] = day_pnl / top_k  # 1/top_k allocation; unfilled slots stay flat
        return cast(Sequence[float], marks)


def build_sip_orb_backtesters(
    *,
    research_store: BarStore,
    holdout_store: HoldoutStore,
    universe: Sequence[str],
) -> tuple[Backtester, Backtester]:
    """The TEST-3-critical pair: each side reads ONLY its own store; disjoint roots
    asserted; the RVOL warmup runs inside each side's own era."""
    assert_disjoint_roots(research_store.root, holdout_store.root)

    def _research(symbol: str) -> Sequence[Bar]:
        return research_store.read_bars(
            symbol=symbol, venue=Venue.NSE, interval_seconds=_INTERVAL_S
        )

    def _holdout(symbol: str) -> Sequence[Bar]:
        return holdout_store.read_holdout(
            symbol=symbol, venue=Venue.NSE, interval_seconds=_INTERVAL_S
        )

    return (
        SipOrbBacktester(_research, universe),
        SipOrbBacktester(_holdout, universe),
    )
