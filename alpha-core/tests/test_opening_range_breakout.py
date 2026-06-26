"""Opening-range breakout strategy tests (Phase 4 / first real edge)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alpha_core.core.enums import AssetClass, Side, Venue
from alpha_core.core.models import Bar
from alpha_core.strategy.examples.opening_range_breakout import (
    OpeningRangeBreakout,
    OpeningRangeBreakoutConfig,
)

# NSE open ~09:15 IST == 03:45 UTC.
OPEN = datetime(2026, 6, 15, 3, 45, tzinfo=UTC)
SYMBOL = "NSE:RELIANCE"
MIN = timedelta(minutes=1)


def _bar(minute: int, o: str, h: str, low: str, c: str, *, day_offset: int = 0) -> Bar:
    start = OPEN + minute * MIN + timedelta(days=day_offset)
    return Bar(
        symbol=SYMBOL,
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        start=start,
        interval=MIN,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal("1000"),
    )


def _cfg(**over: object) -> OpeningRangeBreakoutConfig:
    base = {"opening_range_minutes": 2, "breakout_buffer_bps": Decimal("10")}
    base.update(over)
    return OpeningRangeBreakoutConfig.model_validate(base)


# Two range bars (minute 0,1) -> range high 102, low 98.
_RANGE = [_bar(0, "100", "101", "99", "100"), _bar(1, "100", "102", "98", "100")]


def test_no_signal_while_building_range() -> None:
    strat = OpeningRangeBreakout(_cfg())
    assert all(strat.on_bar(b) == [] for b in _RANGE)


def test_long_entry_on_breakout_above_high() -> None:
    strat = OpeningRangeBreakout(_cfg())
    for b in _RANGE:
        strat.on_bar(b)
    signals = strat.on_bar(_bar(2, "102", "103.5", "101.5", "103"))  # close 103 > 102.102
    assert len(signals) == 1
    assert signals[0].side is Side.BUY
    assert signals[0].quantity == Decimal("1")
    assert "breakout above" in (signals[0].reason or "")


def test_short_entry_on_breakdown_below_low() -> None:
    strat = OpeningRangeBreakout(_cfg())
    for b in _RANGE:
        strat.on_bar(b)
    signals = strat.on_bar(_bar(2, "98", "98.5", "96.5", "97"))  # close 97 < 97.902
    assert len(signals) == 1
    assert signals[0].side is Side.SELL


def test_no_entry_within_range() -> None:
    strat = OpeningRangeBreakout(_cfg())
    for b in _RANGE:
        strat.on_bar(b)
    assert strat.on_bar(_bar(2, "100", "101", "99", "100")) == []


def test_buffer_blocks_marginal_breakout() -> None:
    # Close 102.05 is above the high (102) but inside the 10 bps cushion (102.102).
    strat = OpeningRangeBreakout(_cfg())
    for b in _RANGE:
        strat.on_bar(b)
    assert strat.on_bar(_bar(2, "101", "102.2", "100.5", "102.05")) == []


def test_single_entry_per_day() -> None:
    strat = OpeningRangeBreakout(_cfg())
    for b in _RANGE:
        strat.on_bar(b)
    assert len(strat.on_bar(_bar(2, "102", "103.5", "101.5", "103"))) == 1  # long
    assert strat.on_bar(_bar(3, "103", "104.5", "102.5", "104")) == []  # no re-entry


def test_long_exits_at_opposite_extreme() -> None:
    strat = OpeningRangeBreakout(_cfg())
    for b in _RANGE:
        strat.on_bar(b)
    strat.on_bar(_bar(2, "102", "103.5", "101.5", "103"))  # enter long
    exit_signals = strat.on_bar(_bar(3, "98", "98.5", "96.5", "97"))  # close 97 < low 98
    assert len(exit_signals) == 1
    assert exit_signals[0].side is Side.SELL
    assert "stop" in (exit_signals[0].reason or "")


def test_new_day_resets_the_range() -> None:
    strat = OpeningRangeBreakout(_cfg())
    for b in _RANGE:
        strat.on_bar(b)
    strat.on_bar(_bar(2, "102", "103.5", "101.5", "103"))  # day-1 entry
    # Day 2: range rebuilds — its first bars produce no signal, prior state gone.
    assert strat.on_bar(_bar(0, "200", "201", "199", "200", day_offset=1)) == []
    assert strat.on_bar(_bar(1, "200", "202", "198", "200", day_offset=1)) == []


def test_deterministic_across_runs() -> None:
    bars = [*_RANGE, _bar(2, "102", "103.5", "101.5", "103"), _bar(3, "98", "98.5", "96.5", "97")]

    def run() -> list[list[tuple[Side, str | None]]]:
        strat = OpeningRangeBreakout(_cfg())
        return [[(s.side, s.reason) for s in strat.on_bar(b)] for b in bars]

    assert run() == run()  # same bars -> same signals, every time


def test_no_look_ahead() -> None:
    from alpha_core.backtest.rigor import audit_no_lookahead

    bars = [
        *_RANGE,
        _bar(2, "102", "103.5", "101.5", "103"),  # long entry
        _bar(3, "103", "104", "102.5", "103.5"),  # hold
        _bar(4, "103", "104", "102.5", "103.2"),  # hold (perturbed by the audit)
    ]
    report = audit_no_lookahead(lambda _bars: OpeningRangeBreakout(_cfg()), bars)
    assert report.passed, report.detail


def test_from_config_loads_yaml() -> None:
    strat = OpeningRangeBreakout.from_config()
    # default config opening_range_minutes=15 -> a 1-min bar at minute 0 just builds.
    assert strat.on_bar(_bar(0, "100", "101", "99", "100")) == []
