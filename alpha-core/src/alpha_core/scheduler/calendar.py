"""Trading calendar — holidays and expiries (ADR 0010).

Holidays are exchange-local dates on which the market does not trade. Crypto
trades every day. Expiries drive F&O rollover/settlement (used by later phases).
Dates here are exchange-local *calendar dates* (not tz-aware instants); the clock
converts instants to the session tz before consulting the calendar.
"""

from __future__ import annotations

from datetime import date


class TradingCalendar:
    """Holiday + expiry calendar for one exchange/segment."""

    def __init__(
        self,
        *,
        holidays: set[date] | None = None,
        expiries: set[date] | None = None,
        trades_weekends: bool = False,
    ) -> None:
        self._holidays = holidays or set()
        self._expiries = expiries or set()
        self._trades_weekends = trades_weekends

    def is_trading_day(self, day: date) -> bool:
        """True iff the market trades on ``day`` (weekday rules + holidays)."""
        if not self._trades_weekends and day.weekday() >= 5:  # Sat/Sun
            return False
        return day not in self._holidays

    def is_expiry(self, day: date) -> bool:
        return day in self._expiries

    def next_expiry(self, on_or_after: date) -> date | None:
        """The earliest expiry on or after ``on_or_after``; None if unknown."""
        future = sorted(e for e in self._expiries if e >= on_or_after)
        return future[0] if future else None

    def days_to_expiry(self, expiry: date, reference: date) -> int:
        """Calendar days from ``reference`` to ``expiry`` (negative if past)."""
        return (expiry - reference).days


def load_trading_calendar(exchange: str) -> TradingCalendar:
    """Build ``exchange``'s calendar from ``config/calendar.yaml`` (ADR 0010).

    The yaml carries exchange-local calendar DATES (the file's ▲ note governs the
    annual refresh). An exchange absent from the file gets an empty calendar —
    weekday rules only — which FAILS SAFE for gating (a missed holiday just means
    an idle, tickless session; it can never suppress a real one)."""
    from alpha_core.helpers.config import load_yaml

    spec = (load_yaml("calendar.yaml").get("exchanges") or {}).get(exchange) or {}
    raw_holidays = spec.get("holidays") or []
    holidays: set[date] = set()
    for value in raw_holidays:
        # yaml parses bare ISO dates to date; quoted ones arrive as str — take both.
        holidays.add(value if isinstance(value, date) else date.fromisoformat(str(value)))
    return TradingCalendar(
        holidays=holidays,
        trades_weekends=bool(spec.get("trades_weekends", False)),
    )
