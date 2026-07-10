"""The G3 MIS square-off unwind family — the late-session mechanical-flow registration
on the NIFTY-100 minute panel (`docs/research/intraday-edge-survey-2026-07-v2.md` §G3).

Hypothesis (the survey's mechanical-flow candidate): Indian brokers force-close MIS
intraday positions in the ~15:15-15:25 IST window. Retail intraday exposure concentrates
WITH the day's move (longs in winners, shorts in losers), so the forced unwind is
mechanically AGAINST it — winners get sold into the close, losers bought back. Template
``squareoff_unwind``: at 15:10 IST each session, the day move ``r`` = decision close /
session open - 1 per name; take the ``top_k`` names by ``|r|`` among those with
``|r| ≥ threshold_pct``; enter AGAINST the move (short winners, long losers) and exit at
the square-off bar — the position itself is MIS, living inside the unwind window it
harvests. Most of the grid is expected to die — the registration is tiny.

**The PORTFOLIO path** (the G1 design verbatim): ONE panel cell, one ledger row per
config; ``1/top_k`` capital per selected name (unfilled slots stay flat); ONE mark per
session over the union trading calendar (the OOS slicer sees time, not trades).

Fixed protocol constants (part of the registration): decision = the first bar starting
at/after 15:10 IST, valid only while it starts before 15:15 (a gappier tape yields no
trade — staleness, not interpolation); **one-bar deferral** on entry (decide on bar
``t``'s close, fill at bar ``t+1``'s close, taker — the G1 convention; a fill landing on
the square-off bar itself books a zero-duration cost-only round trip, conservative);
exit at the first bar at/after 15:25 IST (else the session's last bar — the terminal-
flatten convention); session open = the first bar at/after 09:15 IST (pre-open prints
excluded — a symbol whose tape only BEGINS inside the decision window anchors ``r`` on
that late first bar, a reopen-gap move rather than a day move: declared, rare, and not
systematically edge-inflating); IST = UTC+05:30 fixed. No trailing warmup — every
session is eligible from the first.

Costs both sides from the one config home (``cost_scenarios.equity_intraday_cost_sides``,
≈20.6 bps RT — the full Indian MIS cash stack). Deployable-today execution; the driver
still defaults to ``--holdout-reads skip`` (the NSE|60 holdout window is floor-thinned,
#144 — ~20 sessions is too feeble for a one-shot read; the #169/#179 precedent).

**Survivorship DECLARED** exactly as G1: today's NIFTY-100 core universe
(`docs/research/nifty100-universe-2026-07.txt`), not point-in-time membership.

TEST-3: ``build_squareoff_backtesters`` wires the (in-sample, holdout) pair over the
research ``BarStore`` / gate-only ``HoldoutStore``; disjoint roots asserted.
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
from alpha_core.data.store import BarStore
from alpha_core.research.cost_scenarios import equity_intraday_cost_sides
from alpha_core.research.discovery import Backtester
from alpha_core.research.sip_orb_backtester import _IST, _OPEN_MIN, _SQUARE_OFF_MIN, BarReader
from alpha_core.research.strategist import DecimalRange, StrategyProposal, StrategyTemplate

# The single pre-registered panel cell; the value is a display LABEL only — the
# registered member list is the universe file the driver loads.
SQUAREOFF_CELLS: dict[str, str] = {"nse-squareoff": "NIFTY100-2026-07"}
_INTERVAL_S = 60
_DECISION_MIN = 15 * 60 + 10  # decide on the first bar starting at/after 15:10 IST ...
_DECISION_STALE_MIN = 15 * 60 + 15  # ... valid only while it starts before 15:15


class SquareoffUnwindConfig(BaseModel):
    """Vetted ranges live in ``SQUAREOFF_TEMPLATES`` — the registration."""

    model_config = ConfigDict(extra="forbid")
    threshold_pct: Decimal = Decimal("1")
    top_k: int = 5


class _SquareoffSpec:
    def __init__(self, config: SquareoffUnwindConfig) -> None:
        if not 0 < config.threshold_pct <= 10:
            raise ValueError("threshold_pct must be in (0, 10]")
        if not 2 <= config.top_k <= 20:
            raise ValueError("top_k must be in [2, 20]")
        self.config = config


SQUAREOFF_TEMPLATES: dict[str, StrategyTemplate] = {
    # {1, 2}% day-move threshold x {5, 10} top_k = 4 configs, ONE panel cell = 4 trials.
    # Widening = a NEW pre-registration.
    "squareoff_unwind": StrategyTemplate(
        "squareoff_unwind",
        "squareoff_unwind",
        SquareoffUnwindConfig,
        _SquareoffSpec,
        {
            "threshold_pct": DecimalRange(Decimal("1"), Decimal("2"), Decimal("1")),
            "top_k": DecimalRange(Decimal("5"), Decimal("10"), Decimal("5")),
        },
    ),
}


class _Day(NamedTuple):
    """One symbol-session, pre-digested (floats: the statistics plane)."""

    day_open: float  # the first session bar's open (pre-open prints excluded)
    minutes: np.ndarray  # IST minutes-of-day of bar STARTS, int64, time-ordered
    close: np.ndarray


def _digest_day(bars: Sequence[Bar]) -> _Day | None:
    minutes: list[int] = []
    close: list[float] = []
    day_open: float | None = None
    for b in bars:
        ist = b.start + _IST
        m = ist.hour * 60 + ist.minute
        if m < _OPEN_MIN:
            continue  # pre-open auction prints: not the session tape
        if day_open is None:
            day_open = float(b.open)
        minutes.append(m)
        close.append(float(b.close))
    if day_open is None or day_open <= 0 or not minutes:
        return None
    return _Day(day_open, np.array(minutes, dtype=np.int64), np.array(close, dtype=np.float64))


def _member_return(day: _Day, side: float, buy_cost: float, sell_cost: float) -> float:
    """One selected member's unwind-window return: one-bar deferred entry after the
    15:10 decision, exit at the square-off bar (or the session's last bar)."""
    n = len(day.minutes)
    dec_idx = int(np.searchsorted(day.minutes, _DECISION_MIN, side="left"))
    if dec_idx >= n or day.minutes[dec_idx] >= _DECISION_STALE_MIN:
        return 0.0  # no decision print inside [15:10, 15:15): no trade
    sq_idx = int(np.searchsorted(day.minutes, _SQUARE_OFF_MIN, side="left"))
    exit_idx = min(sq_idx, n - 1)  # no 15:25 bar => the last bar (terminal flatten)
    entry_idx = dec_idx + 1  # one-bar deferral
    if entry_idx > exit_idx:
        return 0.0  # no bar left to enter on
    entry = day.close[entry_idx]
    if entry <= 0:
        return 0.0  # data artifact; never divide by it
    gross = side * (day.close[exit_idx] / entry - 1.0)
    return float(gross - buy_cost - sell_cost)


def _decision_move(day: _Day) -> float | None:
    """The day move ``r`` at the decision bar's close; None when no valid decision bar."""
    n = len(day.minutes)
    dec_idx = int(np.searchsorted(day.minutes, _DECISION_MIN, side="left"))
    if dec_idx >= n or day.minutes[dec_idx] >= _DECISION_STALE_MIN:
        return None
    return float(day.close[dec_idx] / day.day_open - 1.0)


class SquareoffBacktester:
    """A ``discovery.Backtester`` running the square-off unwind fold on one store side."""

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
                "square-off fold: no minute bars for any universe member (run the ingest)"
            )
        self._digest_cache = digests
        return digests

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        if proposal.window not in SQUAREOFF_CELLS:
            raise ValueError(
                f"unknown square-off cell {proposal.window!r}; known: {sorted(SQUAREOFF_CELLS)}"
            )
        spec = cast(_SquareoffSpec, SQUAREOFF_TEMPLATES[proposal.template].build(proposal.params))
        theta = float(spec.config.threshold_pct) / 100.0
        top_k = spec.config.top_k

        digests = self._load()
        sessions = sorted({d for days in digests.values() for d in days})
        marks = np.zeros(len(sessions), dtype=np.float64)
        for i, d in enumerate(sessions):
            candidates: list[tuple[float, str, _Day, float]] = []
            for symbol, days in digests.items():
                day = days.get(d)
                if day is None:
                    continue
                r = _decision_move(day)
                if r is not None and abs(r) >= theta:
                    candidates.append((abs(r), symbol, day, -float(np.sign(r))))
            if candidates:
                candidates.sort(key=lambda c: (-c[0], c[1]))  # deterministic: |r| desc, symbol
                day_pnl = sum(
                    _member_return(day, side, self._buy_cost, self._sell_cost)
                    for _, _, day, side in candidates[:top_k]
                )
                marks[i] = day_pnl / top_k  # 1/top_k allocation; unfilled slots stay flat
        return cast(Sequence[float], marks)


def build_squareoff_backtesters(
    *,
    research_store: BarStore,
    holdout_store: HoldoutStore,
    universe: Sequence[str],
) -> tuple[Backtester, Backtester]:
    """The TEST-3-critical pair: each side reads ONLY its own store; disjoint roots
    asserted."""
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
        SquareoffBacktester(_research, universe),
        SquareoffBacktester(_holdout, universe),
    )
