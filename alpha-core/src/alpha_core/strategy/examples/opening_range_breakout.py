"""Opening-range breakout (ORB) — the first real edge (ADR 0001).

Intraday, bar-driven, venue-agnostic, deterministic. For each symbol on each
trading day it:

1. **Builds the opening range** over the first ``opening_range_minutes`` of the
   session — the high/low printed in that window.
2. **Enters once** on the first bar that *closes* beyond the range, with a
   ``breakout_buffer_bps`` cushion: a close above the high → go long; below the
   low → go short. One entry per symbol per day.
3. **Exits at the opposite extreme** (the classic ORB stop): a long exits if a
   later bar closes below the range low; a short exits if it closes above the
   high. Anything still open at the cutoff is handled by the session square-off.

The strategy holds no clock and imports no venue: it infers the session start
from the first bar of each day (``bar.start`` date), so the same code runs in
backtest and live (ADR 0001). It uses only the current bar's close and the prior
range — no look-ahead — and is pure given the bar sequence (no RNG).

All tunables come from ``config/strategies/opening_range_breakout.yaml`` (ADR
0011); nothing is hard-coded here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from alpha_core.core.enums import OrderType, Side
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Signal, Tick
from alpha_core.helpers.config import load_yaml

_BPS = Decimal(10_000)


class OpeningRangeBreakoutConfig(BaseModel):
    """ORB tunables (one home: ``config/strategies/opening_range_breakout.yaml``)."""

    model_config = ConfigDict(extra="forbid")
    strategy_id: str = "opening_range_breakout"
    opening_range_minutes: int = 15
    breakout_buffer_bps: Decimal = Decimal("10")
    quantity: Decimal = Decimal("1")


@dataclass
class _DayState:
    """Per-symbol state for one trading day."""

    day: date
    range_end: datetime  # the instant the opening range closes
    high: Decimal
    low: Decimal
    locked: bool = False  # the opening range is finalized
    entered: bool = False  # already took this day's single entry
    position: Side | None = None  # current open direction, if any


class OpeningRangeBreakout(Strategy):
    """Opening-range breakout (see module docstring)."""

    def __init__(self, config: OpeningRangeBreakoutConfig | None = None) -> None:
        self._cfg = config or OpeningRangeBreakoutConfig()
        if self._cfg.opening_range_minutes <= 0:
            raise ValueError("opening_range_minutes must be positive")
        self._state: dict[str, _DayState] = {}

    @classmethod
    def from_config(cls) -> OpeningRangeBreakout:
        """Build from ``config/strategies/opening_range_breakout.yaml`` (ADR 0011)."""
        raw = load_yaml("strategies/opening_range_breakout.yaml")
        return cls(OpeningRangeBreakoutConfig.model_validate(raw))

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        state = self._state.get(bar.symbol)
        if state is None or bar.start.date() != state.day:
            state = self._start_day(bar)
            self._state[bar.symbol] = state

        if not state.locked:
            if bar.start < state.range_end:  # still inside the opening range
                state.high = max(state.high, bar.high)
                state.low = min(state.low, bar.low)
                return []
            state.locked = True  # first bar past the range -> evaluate it below

        if not state.entered:
            return self._maybe_enter(bar, state)
        return self._maybe_exit(bar, state)

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []

    # --- internals -------------------------------------------------------------

    def _start_day(self, bar: Bar) -> _DayState:
        window = timedelta(minutes=self._cfg.opening_range_minutes)
        return _DayState(
            day=bar.start.date(),
            range_end=bar.start + window,
            high=bar.high,
            low=bar.low,
        )

    def _maybe_enter(self, bar: Bar, state: _DayState) -> Sequence[Signal]:
        buffer = self._cfg.breakout_buffer_bps / _BPS
        up = state.high * (Decimal(1) + buffer)
        down = state.low * (Decimal(1) - buffer)
        if bar.close > up:
            state.entered, state.position = True, Side.BUY
            score = (bar.close - up) / up  # how far beyond the breakout level
            return [self._signal(bar, Side.BUY, "ORB: breakout above opening range", score)]
        if bar.close < down:
            state.entered, state.position = True, Side.SELL
            score = (down - bar.close) / down
            return [self._signal(bar, Side.SELL, "ORB: breakdown below opening range", score)]
        return []

    def _maybe_exit(self, bar: Bar, state: _DayState) -> Sequence[Signal]:
        if state.position is Side.BUY and bar.close < state.low:
            state.position = None
            return [self._signal(bar, Side.SELL, "ORB: stop at opening-range low", None)]
        if state.position is Side.SELL and bar.close > state.high:
            state.position = None
            return [self._signal(bar, Side.BUY, "ORB: stop at opening-range high", None)]
        return []

    def _signal(self, bar: Bar, side: Side, reason: str, score: Decimal | None) -> Signal:
        # Scored entries carry conviction (for cross-sectional ranking, D8); exits
        # are score=None (a close, not a fresh directional bet) — the SignalBook
        # convention the portfolio engine relies on.
        return Signal(
            strategy_id=self._cfg.strategy_id,
            symbol=bar.symbol,
            asset_class=bar.asset_class,
            side=side,
            quantity=self._cfg.quantity,
            order_type=OrderType.MARKET,
            created_at=bar.start + bar.interval,  # decision at bar close
            reason=reason,
            score=score,
        )
