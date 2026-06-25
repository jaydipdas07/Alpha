"""Scheduler tests: clocks, market schedule, calendar (ADR 0010)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from alpha_core.core.enums import AssetClass
from alpha_core.scheduler.calendar import TradingCalendar
from alpha_core.scheduler.clock import FakeClock, MarketSchedule, SystemClock, schedule_for

IST = "Asia/Kolkata"


def _ist(year: int, month: int, day: int, hh: int, mm: int) -> datetime:
    # Build a UTC instant corresponding to a given IST wall time (IST = UTC+5:30).
    from zoneinfo import ZoneInfo

    return datetime(year, month, day, hh, mm, tzinfo=ZoneInfo(IST)).astimezone(UTC)


# --- clocks --------------------------------------------------------------------


def test_system_clock_is_utc() -> None:
    assert SystemClock().now().tzinfo is UTC


def test_fake_clock_advance_and_set() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    c = FakeClock(start)
    assert c.now() == start
    c.advance(timedelta(hours=2))
    assert c.now() == start + timedelta(hours=2)
    c.set(datetime(2026, 6, 15, 9, 0, tzinfo=UTC))
    assert c.now() == datetime(2026, 6, 15, 9, 0, tzinfo=UTC)


def test_fake_clock_requires_tzaware() -> None:
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 1, 1))


# --- equity session schedule ---------------------------------------------------


def _nse(calendar: TradingCalendar | None = None) -> MarketSchedule:
    return MarketSchedule(
        tz=IST,
        open_time=time(9, 15),
        close_time=time(15, 30),
        no_new_entry=time(15, 10),
        square_off=time(15, 15),
        calendar=calendar,
    )


def test_session_open_and_closed() -> None:
    nse = _nse()
    # 2026-06-15 is a Monday.
    assert nse.is_open(_ist(2026, 6, 15, 10, 0)) is True
    assert nse.is_open(_ist(2026, 6, 15, 9, 0)) is False  # pre-open
    assert nse.is_open(_ist(2026, 6, 15, 16, 0)) is False  # after close


def test_session_closed_on_weekend() -> None:
    nse = _nse()
    # 2026-06-13 is a Saturday.
    assert nse.is_open(_ist(2026, 6, 13, 10, 0)) is False


def test_session_closed_on_holiday() -> None:
    cal = TradingCalendar(holidays={date(2026, 6, 15)})
    nse = _nse(cal)
    assert nse.is_open(_ist(2026, 6, 15, 10, 0)) is False


def test_no_new_entry_cutoff() -> None:
    nse = _nse()
    assert nse.is_after_no_new_entry(_ist(2026, 6, 15, 15, 5)) is False
    assert nse.is_after_no_new_entry(_ist(2026, 6, 15, 15, 12)) is True


def test_square_off_cutoff() -> None:
    nse = _nse()
    assert nse.is_at_or_after_square_off(_ist(2026, 6, 15, 15, 10)) is False
    assert nse.is_at_or_after_square_off(_ist(2026, 6, 15, 15, 16)) is True


# --- crypto 24/7 ---------------------------------------------------------------


def test_crypto_always_open_no_squareoff() -> None:
    crypto = MarketSchedule(is_24x7=True)
    sunday_3am = datetime(2026, 6, 14, 3, 0, tzinfo=UTC)
    assert crypto.is_open(sunday_3am) is True
    assert crypto.is_after_no_new_entry(sunday_3am) is False
    assert crypto.is_at_or_after_square_off(sunday_3am) is False


def test_session_requires_hours() -> None:
    with pytest.raises(ValueError):
        MarketSchedule(tz=IST)  # not 24x7 but no open/close


# --- calendar ------------------------------------------------------------------


def test_calendar_trading_day_and_expiry() -> None:
    cal = TradingCalendar(
        holidays={date(2026, 6, 15)},
        expiries={date(2026, 6, 25), date(2026, 7, 30)},
    )
    assert cal.is_trading_day(date(2026, 6, 16)) is True
    assert cal.is_trading_day(date(2026, 6, 15)) is False  # holiday
    assert cal.is_trading_day(date(2026, 6, 14)) is False  # Sunday
    assert cal.next_expiry(date(2026, 6, 20)) == date(2026, 6, 25)
    assert cal.days_to_expiry(date(2026, 6, 25), date(2026, 6, 20)) == 5


def test_calendar_weekend_trading_for_crypto() -> None:
    cal = TradingCalendar(trades_weekends=True)
    assert cal.is_trading_day(date(2026, 6, 14)) is True  # Sunday


# --- schedule_for factory (SCHED-1: build the session schedule from config) -----


def test_schedule_for_crypto_is_24x7() -> None:
    s = schedule_for(frozenset({AssetClass.CRYPTO}))
    assert s.is_24x7 is True
    t = _ist(2026, 6, 15, 20, 0)  # late evening, well past any equity session
    assert s.is_after_no_new_entry(t) is False
    assert s.is_at_or_after_square_off(t) is False


def test_schedule_for_equity_uses_nse_session() -> None:
    s = schedule_for(frozenset({AssetClass.EQUITY}))
    assert s.is_24x7 is False
    assert s.is_open(_ist(2026, 6, 15, 10, 0)) is True
    # 15:12 IST: past no-new-entry (15:10), before square-off (15:15)
    assert s.is_after_no_new_entry(_ist(2026, 6, 15, 15, 12)) is True
    assert s.is_at_or_after_square_off(_ist(2026, 6, 15, 15, 12)) is False
    # 15:20 IST: at/after square-off
    assert s.is_at_or_after_square_off(_ist(2026, 6, 15, 15, 20)) is True


def test_schedule_for_index_option_uses_session() -> None:
    s = schedule_for(frozenset({AssetClass.INDEX_OPTION}))
    assert s.is_24x7 is False
    assert s.is_at_or_after_square_off(_ist(2026, 6, 15, 15, 20)) is True
