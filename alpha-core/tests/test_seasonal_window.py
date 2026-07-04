"""F2 seasonality templates — window timing, trend direction, gap robustness, the registry's
pre-registered space (exhaustive + tiny), and config validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alpha_core.core.enums import AssetClass, Side, Venue
from alpha_core.core.models import Bar, Signal
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.seasonal_templates import SEASONAL_TEMPLATES
from alpha_core.research.strategist import CellSaturated, RandomProposer, Strategist
from alpha_core.strategy.examples.seasonal_window import (
    SeasonalHourLong,
    SeasonalHourLongConfig,
    SeasonalSundayTrend,
    SeasonalSundayTrendConfig,
)

# 2026-06-06 is a Saturday; 06-07 a Sunday (UTC) — the Sunday-template fixtures hang off it.
_SAT = datetime(2026, 6, 6, 0, 0, tzinfo=UTC)


def _hbar(i: int, close: str, *, start: datetime = _SAT) -> Bar:
    """Hourly bar #i from ``start`` (closes at start + (i+1) hours)."""
    c = Decimal(close)
    return Bar(
        symbol="BTCUSDT",
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        start=start + timedelta(hours=i),
        interval=timedelta(hours=1),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=Decimal("10"),
    )


def _run(strategy: SeasonalHourLong | SeasonalSundayTrend, bars: list[Bar]) -> list[Signal]:
    out: list[Signal] = []
    for b in bars:
        out.extend(strategy.on_bar(b))
    return out


# --- SeasonalHourLong -----------------------------------------------------------------


def test_hour_long_enters_at_window_open_and_exits_after_hold() -> None:
    s = SeasonalHourLong(SeasonalHourLongConfig(hour_start=21, hold_hours=2))
    bars = [_hbar(i, "100") for i in range(50)]  # Sat 00:00 .. Mon 02:00 closes
    signals = _run(s, bars)
    assert [(sig.side, sig.created_at.hour) for sig in signals[:2]] == [
        (Side.BUY, 21),
        (Side.SELL, 23),
    ]
    # entry is a scored conviction; the exit is a close (score=None, the SignalBook convention)
    assert signals[0].score is not None and signals[1].score is None
    # the next day's window fires again — a daily template
    assert (signals[2].side, signals[2].created_at.hour) == (Side.BUY, 21)
    assert signals[2].created_at.date() > signals[0].created_at.date()


def test_hour_long_window_wraps_past_midnight() -> None:
    s = SeasonalHourLong(SeasonalHourLongConfig(hour_start=22, hold_hours=4))
    bars = [_hbar(i, "100") for i in range(30)]
    signals = _run(s, bars)
    # the first bar (closes 01:00 Sat) is already inside the wrapped {22,23,0,1} window ->
    # immediate partial entry, exited 4h later at 05:00 (the mid-window-start behaviour)
    assert (signals[0].side, signals[0].created_at.hour) == (Side.BUY, 1)
    assert (signals[1].side, signals[1].created_at.hour) == (Side.SELL, 5)
    # the first FULL window then wraps midnight: enter 22:00, exit 02:00 the next day
    assert (signals[2].side, signals[2].created_at.hour) == (Side.BUY, 22)
    assert (signals[3].side, signals[3].created_at.hour) == (Side.SELL, 2)
    assert signals[3].created_at.date() > signals[2].created_at.date()


def test_hour_long_gap_delays_exit_to_next_bar_never_wedges() -> None:
    s = SeasonalHourLong(SeasonalHourLongConfig(hour_start=21, hold_hours=2))
    bars = [_hbar(i, "100") for i in range(50)]
    # drop the exit bar (the one closing at 23:00, i=22) and the next few hours
    gapped = [b for b in bars if not (22 <= (b.start - _SAT) // timedelta(hours=1) <= 25)]
    signals = _run(s, gapped)
    assert signals[0].side is Side.BUY
    assert signals[1].side is Side.SELL  # exits on the first bar AFTER the gap (elapsed >= hold)
    assert signals[1].created_at.hour == 3
    # and the position state is clean: the following day's window re-enters normally
    assert signals[2].side is Side.BUY


def test_hour_long_first_bar_mid_window_enters_immediately() -> None:
    s = SeasonalHourLong(SeasonalHourLongConfig(hour_start=21, hold_hours=3))
    late_start = _SAT + timedelta(hours=21)  # first bar closes 22:00, inside [21, 24)
    bars = [_hbar(i, "100", start=late_start) for i in range(6)]
    signals = _run(s, bars)
    assert (signals[0].side, signals[0].created_at.hour) == (Side.BUY, 22)


@pytest.mark.parametrize(
    "kwargs",
    [{"hour_start": 24}, {"hour_start": -1}, {"hold_hours": 0}, {"hold_hours": 24}],
)
def test_hour_long_rejects_invalid_config(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        SeasonalHourLong(SeasonalHourLongConfig.model_validate(kwargs))


# --- SeasonalSundayTrend --------------------------------------------------------------


def _sunday_bars(step: str, n: int = 80) -> list[Bar]:
    """Hourly closes moving by ``step`` per bar from Saturday 00:00 (Sun entry + Mon exit)."""
    return [_hbar(i, str(Decimal("100") + Decimal(step) * i)) for i in range(n)]


def test_sunday_trend_goes_long_with_rising_lookback_and_exits_after_hold() -> None:
    s = SeasonalSundayTrend(
        SeasonalSundayTrendConfig(entry_hour=22, hold_hours=24, trend_lookback_days=1)
    )
    signals = _run(s, _sunday_bars("1"))
    assert len(signals) == 2
    entry, exit_ = signals
    assert entry.side is Side.BUY and entry.created_at.weekday() == 6  # Sunday
    assert entry.created_at.hour == 22 and entry.score is not None
    assert exit_.side is Side.SELL and exit_.score is None
    assert exit_.created_at - entry.created_at == timedelta(hours=24)


def test_sunday_trend_goes_short_with_falling_lookback() -> None:
    s = SeasonalSundayTrend(
        SeasonalSundayTrendConfig(entry_hour=22, hold_hours=24, trend_lookback_days=1)
    )
    signals = _run(s, _sunday_bars("-1"))
    assert signals[0].side is Side.SELL and signals[1].side is Side.BUY


def test_sunday_trend_skips_entry_during_warmup_and_when_flat() -> None:
    cfg = SeasonalSundayTrendConfig(entry_hour=22, hold_hours=24, trend_lookback_days=3)
    # only 2 days of history before Sunday 22:00 -> no close old enough -> no entry
    assert _run(SeasonalSundayTrend(cfg), _sunday_bars("1", n=50)) == []
    # dead-flat lookback (ref == close) -> no direction -> no entry
    flat = SeasonalSundayTrend(
        SeasonalSundayTrendConfig(entry_hour=22, hold_hours=24, trend_lookback_days=1)
    )
    assert _run(flat, [_hbar(i, "100") for i in range(80)]) == []


def test_sunday_trend_enters_once_per_week_on_finer_bars() -> None:
    s = SeasonalSundayTrend(
        SeasonalSundayTrendConfig(entry_hour=22, hold_hours=24, trend_lookback_days=1)
    )
    # 30-minute bars: two bars close inside Sunday hour 22 (22:00 and 22:30) — one entry only
    bars = []
    for i in range(60 * 4):
        c = Decimal("100") + Decimal(i)
        bars.append(
            Bar(
                symbol="BTCUSDT",
                venue=Venue.BINANCE,
                asset_class=AssetClass.CRYPTO,
                start=_SAT + timedelta(minutes=30 * i),
                interval=timedelta(minutes=30),
                open=c,
                high=c,
                low=c,
                close=c,
                volume=Decimal("10"),
            )
        )
    signals = _run(s, bars)
    entries = [sig for sig in signals if sig.score is not None]
    assert len(entries) == 1 and entries[0].created_at.hour == 22


@pytest.mark.parametrize(
    "kwargs",
    [{"entry_hour": 24}, {"hold_hours": 0}, {"hold_hours": 145}, {"trend_lookback_days": 0}],
)
def test_sunday_trend_rejects_invalid_config(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        SeasonalSundayTrend(SeasonalSundayTrendConfig.model_validate(kwargs))


# --- the registry / pre-registered space ----------------------------------------------


def test_registry_builds_at_space_corners() -> None:
    for template in SEASONAL_TEMPLATES.values():
        lo = {name: spec.low for name, spec in template.param_space.items()}
        hi = {name: spec.high for name, spec in template.param_space.items()}
        template.build(lo)
        template.build(hi)


def test_registry_spaces_are_tiny_and_exhaustible() -> None:
    """The pre-registration: exactly 9 configs per template per cell, provably exhausted."""
    with ProposalLedger() as ledger:
        strategist = Strategist(
            ledger,
            proposer=RandomProposer(seed=10),
            templates=SEASONAL_TEMPLATES,
            max_attempts=500,
        )
        for name in SEASONAL_TEMPLATES:
            count = 0
            while True:
                try:
                    strategist.propose(name, market=AssetClass.CRYPTO, window="btcusdt-1h")
                except CellSaturated:
                    break
                count += 1
            assert count == 9, f"{name}: pre-registered space changed size ({count} != 9)"


def test_default_configs_load_from_yaml() -> None:
    assert SeasonalHourLong.from_config()._cfg.hour_start == 21
    assert SeasonalSundayTrend.from_config()._cfg.entry_hour == 22
