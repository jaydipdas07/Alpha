"""Crypto time-of-day / day-of-week seasonality templates (F2, the intraday-mandate family —
`docs/research/intraday-edge-survey-2026-07.md` §2.4).

Two literature-pinned, bar-driven, deterministic strategies over hourly (or finer) bars:

- ``SeasonalHourLong`` — long a fixed daily UTC window. The documented anomaly is the late-UTC
  window (QuantPedia/Padysák-Vojtko: 21:00→23:00 UTC long on BTC, 2015-2022), when every major
  traditional exchange is closed. Long-only by design — the prior has a direction; a short
  variant would be a new pre-registered family, not a knob.
- ``SeasonalSundayTrend`` — the "Monday Asia open" effect (Concretum, 2018-2025): from late
  Sunday UTC, high-frequency trend persists ~24h. Enters at a fixed Sunday UTC hour in the
  direction of the trailing ``trend_lookback_days`` return, holds ``hold_hours``.

Both hold no clock (decisions at ``bar.start + bar.interval`` — bar-time only, TEST-1), use only
closes up to the current bar (no look-ahead), and exit with an opposite-side ``score=None``
signal (the SignalBook close convention). Exits are **elapsed-based** (entry time + hold), so a
data gap can only delay an exit to the next bar — never wedge a position or double-enter.

Tunables: ``config/strategies/seasonal_hour_long.yaml`` / ``seasonal_sunday_trend.yaml``
(ADR 0011); the vetted discovery ranges live in ``research/seasonal_templates.py``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from alpha_core.core.enums import OrderType, Side
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Signal, Tick
from alpha_core.helpers.config import load_yaml

_SUNDAY = 6  # datetime.weekday(): Monday=0 .. Sunday=6


def _in_window(hour: int, start: int, length: int) -> bool:
    """True iff ``hour`` falls in the daily window ``[start, start+length)`` mod 24."""
    return (hour - start) % 24 < length


class SeasonalHourLongConfig(BaseModel):
    """Daily fixed-UTC-window long (``config/strategies/seasonal_hour_long.yaml``)."""

    model_config = ConfigDict(extra="forbid")
    strategy_id: str = "seasonal_hour_long"
    hour_start: int = 21  # UTC hour the long window opens (position on from this instant)
    hold_hours: int = 2  # window length in hours (also the elapsed-exit horizon)
    quantity: Decimal = Decimal("1")
    # "taker" (default, the frozen-verdict form): MARKET entries/exits. "post_only" (the
    # maker fill model): the entry rests a post-only LIMIT at the decision close, alive
    # exactly through the NEXT bar (miss = no trade that window); the exit is a MARKET
    # reduce-only (guaranteed flat; a missed entry can never be inverted into a short).
    entry_execution: Literal["taker", "post_only"] = "taker"


class SeasonalSundayTrendConfig(BaseModel):
    """Sunday-entry weekly trend window (``config/strategies/seasonal_sunday_trend.yaml``)."""

    model_config = ConfigDict(extra="forbid")
    strategy_id: str = "seasonal_sunday_trend"
    entry_hour: int = 22  # UTC hour on SUNDAY the window opens
    hold_hours: int = 24  # holding horizon (fixed by the family pre-registration, not searched)
    trend_lookback_days: int = 1  # direction = sign of the trailing N-day return at entry
    quantity: Decimal = Decimal("1")
    entry_execution: Literal["taker", "post_only"] = "taker"  # see SeasonalHourLongConfig


@dataclass
class _HourState:
    entered_at: datetime | None = None


@dataclass
class _SundayState:
    closes: deque[tuple[datetime, Decimal]]
    entered_at: datetime | None = None
    side: Side | None = None
    last_entry_key: tuple[int, int] | None = None  # (iso year, iso week) — one entry per week


class SeasonalHourLong(Strategy):
    """Long a fixed daily UTC window (see module docstring)."""

    def __init__(self, config: SeasonalHourLongConfig | None = None) -> None:
        self._cfg = config or SeasonalHourLongConfig()
        if not 0 <= self._cfg.hour_start <= 23:
            raise ValueError("hour_start must be in [0, 23]")
        if not 1 <= self._cfg.hold_hours <= 23:
            raise ValueError("hold_hours must be in [1, 23] (the window must be intra-day)")
        if self._cfg.entry_execution == "post_only" and self._cfg.hold_hours < 2:
            # #184 review F3: a 1-bar window under maker entries can fill on the exit
            # bar's own ticks BEFORE the exit sizes itself (undrained fill -> reduce-only
            # noop -> orphaned position). The registered space is {2,3}h; refuse below it.
            raise ValueError("post_only entries need hold_hours >= 2 (same-bar fill/exit race)")
        self._state: dict[str, _HourState] = {}

    @classmethod
    def from_config(cls) -> SeasonalHourLong:
        raw = load_yaml("strategies/seasonal_hour_long.yaml")
        return cls(SeasonalHourLongConfig.model_validate(raw))

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        st = self._state.setdefault(bar.symbol, _HourState())
        now = bar.start + bar.interval  # the decision instant (bar close)
        if st.entered_at is None:
            # enter on the first bar that closes inside the window (an hourly bar closing at
            # exactly hour_start:00 is the canonical case; finer bars enter at the first close
            # in-window, so the same template runs at any intra-day frequency).
            if _in_window(now.hour, self._cfg.hour_start, self._cfg.hold_hours):
                st.entered_at = now
                return [self._entry(bar, Side.BUY, "seasonal window open")]
            return []
        if now - st.entered_at >= timedelta(hours=self._cfg.hold_hours):
            st.entered_at = None
            return [self._exit(bar, Side.SELL, "seasonal window close")]
        return []

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []

    def _entry(self, bar: Bar, side: Side, reason: str) -> Signal:
        if self._cfg.entry_execution == "post_only":
            # The maker fill model: rest AT the decision close, alive exactly through
            # the NEXT bar (its extremes may trade through); miss = no trade.
            return Signal(
                strategy_id=self._cfg.strategy_id,
                symbol=bar.symbol,
                asset_class=bar.asset_class,
                side=side,
                quantity=self._cfg.quantity,
                order_type=OrderType.LIMIT,
                limit_price=bar.close,
                post_only=True,
                valid_until=bar.start + 2 * bar.interval,
                created_at=bar.start + bar.interval,
                reason=reason,
                score=Decimal(1),
            )
        return self._signal(bar, side, reason, Decimal(1))

    def _exit(self, bar: Bar, side: Side, reason: str) -> Signal:
        if self._cfg.entry_execution == "post_only":
            # Taker reduce-only: guaranteed flat, and a MISSED entry can never be
            # inverted into a fresh position by its own exit (#179's bug class).
            return Signal(
                strategy_id=self._cfg.strategy_id,
                symbol=bar.symbol,
                asset_class=bar.asset_class,
                side=side,
                quantity=self._cfg.quantity,
                order_type=OrderType.MARKET,
                reduce_only=True,
                created_at=bar.start + bar.interval,
                reason=reason,
                score=None,
            )
        return self._signal(bar, side, reason, None)

    def _signal(self, bar: Bar, side: Side, reason: str, score: Decimal | None) -> Signal:
        return Signal(
            strategy_id=self._cfg.strategy_id,
            symbol=bar.symbol,
            asset_class=bar.asset_class,
            side=side,
            quantity=self._cfg.quantity,
            order_type=OrderType.MARKET,
            created_at=bar.start + bar.interval,
            reason=reason,
            score=score,
        )


class SeasonalSundayTrend(Strategy):
    """Sunday-entry weekly trend window (see module docstring)."""

    def __init__(self, config: SeasonalSundayTrendConfig | None = None) -> None:
        self._cfg = config or SeasonalSundayTrendConfig()
        if not 0 <= self._cfg.entry_hour <= 23:
            raise ValueError("entry_hour must be in [0, 23]")
        if not 1 <= self._cfg.hold_hours <= 144:
            raise ValueError("hold_hours must be in [1, 144] (must exit before the next Sunday)")
        if self._cfg.trend_lookback_days < 1:
            raise ValueError("trend_lookback_days must be >= 1")
        self._state: dict[str, _SundayState] = {}

    @classmethod
    def from_config(cls) -> SeasonalSundayTrend:
        raw = load_yaml("strategies/seasonal_sunday_trend.yaml")
        return cls(SeasonalSundayTrendConfig.model_validate(raw))

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        st = self._state.get(bar.symbol)
        if st is None:
            # history depth: the lookback in HOURLY anchors + slack so a gappy series still
            # finds a reference close at (or just older than) the lookback horizon.
            depth = self._cfg.trend_lookback_days * 24 + 8
            st = _SundayState(closes=deque(maxlen=depth))
            self._state[bar.symbol] = st
        now = bar.start + bar.interval
        # store at most one close per hour: the trend reference stays hourly-granular (and the
        # buffer depth correct) whatever the cell's bar interval — finer bars just skip appends.
        if not st.closes or now - st.closes[-1][0] >= timedelta(hours=1):
            st.closes.append((now, bar.close))

        if st.entered_at is not None:
            assert st.side is not None
            if now - st.entered_at >= timedelta(hours=self._cfg.hold_hours):
                exit_side = Side.SELL if st.side is Side.BUY else Side.BUY
                st.entered_at, st.side = None, None
                return [self._exit(bar, exit_side, "seasonal trend window close")]
            return []

        if now.weekday() != _SUNDAY or now.hour != self._cfg.entry_hour:
            return []
        week = now.isocalendar()[:2]
        if st.last_entry_key == week:
            return []  # one entry per week — a finer-than-hourly series triggers once, not 60x
        ref = self._reference_close(st, now)
        if ref is None or ref == bar.close:
            return []  # warmup (no close old enough) or a dead-flat lookback: no direction
        side = Side.BUY if bar.close > ref else Side.SELL
        st.entered_at, st.side, st.last_entry_key = now, side, week
        return [self._entry(bar, side, "seasonal trend window open")]

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []

    def _reference_close(self, st: _SundayState, now: datetime) -> Decimal | None:
        """The newest stored close at least ``trend_lookback_days`` old (None during warmup)."""
        cutoff = now - timedelta(days=self._cfg.trend_lookback_days)
        for ts, close in reversed(st.closes):
            if ts <= cutoff:
                return close
        return None

    def _entry(self, bar: Bar, side: Side, reason: str) -> Signal:
        if self._cfg.entry_execution == "post_only":
            # The maker fill model: rest AT the decision close, alive exactly through
            # the NEXT bar (its extremes may trade through); miss = no trade.
            return Signal(
                strategy_id=self._cfg.strategy_id,
                symbol=bar.symbol,
                asset_class=bar.asset_class,
                side=side,
                quantity=self._cfg.quantity,
                order_type=OrderType.LIMIT,
                limit_price=bar.close,
                post_only=True,
                valid_until=bar.start + 2 * bar.interval,
                created_at=bar.start + bar.interval,
                reason=reason,
                score=Decimal(1),
            )
        return self._signal(bar, side, reason, Decimal(1))

    def _exit(self, bar: Bar, side: Side, reason: str) -> Signal:
        if self._cfg.entry_execution == "post_only":
            # Taker reduce-only: guaranteed flat, and a MISSED entry can never be
            # inverted into a fresh position by its own exit (#179's bug class).
            return Signal(
                strategy_id=self._cfg.strategy_id,
                symbol=bar.symbol,
                asset_class=bar.asset_class,
                side=side,
                quantity=self._cfg.quantity,
                order_type=OrderType.MARKET,
                reduce_only=True,
                created_at=bar.start + bar.interval,
                reason=reason,
                score=None,
            )
        return self._signal(bar, side, reason, None)

    def _signal(self, bar: Bar, side: Side, reason: str, score: Decimal | None) -> Signal:
        return Signal(
            strategy_id=self._cfg.strategy_id,
            symbol=bar.symbol,
            asset_class=bar.asset_class,
            side=side,
            quantity=self._cfg.quantity,
            order_type=OrderType.MARKET,
            created_at=bar.start + bar.interval,
            reason=reason,
            score=score,
        )
