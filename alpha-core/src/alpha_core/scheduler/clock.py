"""Clocks and per-segment market schedule (ADR 0010).

Internal time is tz-aware UTC (ADR 0002); session rules are expressed in the
exchange tz and converted at the edge here. Two clocks: ``SystemClock`` (real)
and ``FakeClock`` (advanceable, for deterministic tests).

``MarketSchedule`` answers session questions for one segment:
- equity / index_option: a daily session with no-new-entry + square-off cutoffs;
- crypto: 24/7, no square-off.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from alpha_core.core.enums import AssetClass
from alpha_core.helpers.config import ConfigError, load_yaml
from alpha_core.scheduler.calendar import TradingCalendar


@runtime_checkable
class Clock(Protocol):
    """Source of the current tz-aware UTC instant."""

    def now(self) -> datetime: ...


class SystemClock:
    """Wall-clock time in UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """A controllable clock for tests. Always returns tz-aware UTC."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock start must be tz-aware")
        self._t = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._t

    def advance(self, delta: timedelta) -> None:
        self._t += delta

    def set(self, t: datetime) -> None:
        if t.tzinfo is None:
            raise ValueError("FakeClock set must be tz-aware")
        self._t = t.astimezone(UTC)


class MarketSchedule:
    """Session rules for one segment, evaluated at tz-aware UTC instants."""

    def __init__(
        self,
        *,
        tz: tzinfo | str = UTC,
        open_time: time | None = None,
        close_time: time | None = None,
        no_new_entry: time | None = None,
        square_off: time | None = None,
        calendar: TradingCalendar | None = None,
        is_24x7: bool = False,
    ) -> None:
        self._tz: tzinfo = ZoneInfo(tz) if isinstance(tz, str) else tz
        self._open = open_time
        self._close = close_time
        self._no_new_entry = no_new_entry
        self._square_off = square_off
        self._calendar = calendar or TradingCalendar(trades_weekends=is_24x7)
        self._is_24x7 = is_24x7
        if not is_24x7 and (open_time is None or close_time is None):
            raise ValueError("a session segment requires open_time and close_time")

    @property
    def is_24x7(self) -> bool:
        """True for a 24/7 segment (crypto) — no session cutoffs or square-off."""
        return self._is_24x7

    def _local(self, instant: datetime) -> datetime:
        if instant.tzinfo is None:
            raise ValueError("instant must be tz-aware")
        return instant.astimezone(self._tz)

    def is_open(self, instant: datetime) -> bool:
        """True iff the market is trading at ``instant``."""
        if self._is_24x7:
            return True
        local = self._local(instant)
        if not self._calendar.is_trading_day(local.date()):
            return False
        assert self._open is not None and self._close is not None
        return self._open <= local.time() <= self._close

    def is_after_no_new_entry(self, instant: datetime) -> bool:
        """True iff new entries should be blocked (past the cutoff) this session."""
        if self._is_24x7 or self._no_new_entry is None:
            return False
        local = self._local(instant)
        if not self._calendar.is_trading_day(local.date()):
            return False
        return local.time() >= self._no_new_entry

    def is_at_or_after_square_off(self, instant: datetime) -> bool:
        """True iff intraday positions should be squared off by now this session."""
        if self._is_24x7 or self._square_off is None:
            return False
        local = self._local(instant)
        if not self._calendar.is_trading_day(local.date()):
            return False
        return local.time() >= self._square_off


def schedule_for(asset_classes: frozenset[AssetClass]) -> MarketSchedule:
    """Build the session schedule for a venue's asset classes from instruments.yaml
    (ADR 0010, the ``segments`` block).

    A process trades one venue (ADR 0011 isolation), so one schedule governs its
    loop. Crypto is 24/7 (no cutoffs, no square-off); equity and index_option share
    the NSE session. Until now these ``segments`` rules were never read — this is
    the consumer that makes them live (SCHED-1).
    """
    if asset_classes and all(c is AssetClass.CRYPTO for c in asset_classes):
        return MarketSchedule(is_24x7=True)
    segments = load_yaml("instruments.yaml").get("segments") or {}
    seg_name = "equity" if AssetClass.EQUITY in asset_classes else "index_option"
    seg = segments.get(seg_name)
    if not seg or "session" not in seg:
        raise ConfigError(f"instruments.yaml: missing session rules for segment {seg_name!r}")
    session = seg["session"]
    return MarketSchedule(
        tz=session["tz"],
        open_time=time.fromisoformat(session["open"]),
        close_time=time.fromisoformat(session["close"]),
        no_new_entry=time.fromisoformat(seg["no_new_entry_time"]),
        square_off=time.fromisoformat(seg["square_off_time"]),
    )
