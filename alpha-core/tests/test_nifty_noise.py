"""F1 NIFTY noise-area breakout strategy tests (survey §5 — the pre-registered family)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from alpha_core.core.enums import AssetClass, Side, Venue
from alpha_core.core.models import Bar, Signal
from alpha_core.strategy.examples.nifty_noise import NiftyNoiseBreakout, NiftyNoiseBreakoutConfig

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = "NSE:NIFTY 50"
ONE_MIN = timedelta(minutes=1)


def _bar(day: int, hh: int, mm: int, *, open_: str, close: str) -> Bar:
    """A 1m bar CLOSING at ``hh:mm`` IST on 2026-06-``day`` (a June weekday run)."""
    close_ist = datetime(2026, 6, day, hh, mm, tzinfo=IST)
    o, c = Decimal(open_), Decimal(close)
    return Bar(
        symbol=SYMBOL,
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        start=close_ist - ONE_MIN,
        interval=ONE_MIN,
        open=o,
        high=max(o, c) + Decimal("1"),
        low=min(o, c) - Decimal("1"),
        close=c,
        volume=Decimal("1000"),
    )


def _warmup_days(strat: NiftyNoiseBreakout, *days: int) -> None:
    """Feed two plain days per given day-number: open 100, 10:00 move +1%/+2%, 10:30 ditto.

    After two days the (10, 0) and (10, 30) noise histories are FULL for lookback=2 with
    boundary mean(0.01, 0.02) = 0.015 — the exact-band fixture every test below uses.
    """
    moves = ["101", "102"]  # +1% then +2% vs the 100 open
    for day, close in zip(days, moves, strict=True):
        assert strat.on_bar(_bar(day, 9, 16, open_="100", close="100")) == []
        assert strat.on_bar(_bar(day, 10, 0, open_=close, close=close)) == []
        assert strat.on_bar(_bar(day, 10, 30, open_=close, close=close)) == []


def _cfg(**over: object) -> NiftyNoiseBreakoutConfig:
    base: dict[str, object] = {"lookback_days": 2}
    base.update(over)
    return NiftyNoiseBreakoutConfig.model_validate(base)


def _only(signals: Sequence[Signal]) -> Signal:
    (sig,) = signals
    return sig


def test_warmup_days_never_enter() -> None:
    strat = NiftyNoiseBreakout(_cfg())
    # Day 1: a huge +5% move at the first-ever 10:00 check — history is EMPTY, no entry.
    assert strat.on_bar(_bar(15, 9, 16, open_="100", close="100")) == []
    assert strat.on_bar(_bar(15, 10, 0, open_="105", close="105")) == []
    # Day 2: history has ONE day (< lookback 2) — still no entry on another huge move.
    assert strat.on_bar(_bar(16, 9, 16, open_="100", close="100")) == []
    assert strat.on_bar(_bar(16, 10, 0, open_="105", close="105")) == []


def test_break_beyond_the_boundary_enters_with_the_move() -> None:
    strat = NiftyNoiseBreakout(_cfg())
    _warmup_days(strat, 15, 16)
    # Day 3: boundary at 10:00 = mean(1%, 2%) = 1.5%. A +1.4% close stays inside — no
    # entry (and this pins look-ahead: were today's own move in its boundary, the mean
    # would drop to ~1.13% and 1.4% would fire).
    assert strat.on_bar(_bar(17, 9, 16, open_="100", close="100")) == []
    assert strat.on_bar(_bar(17, 10, 0, open_="101.4", close="101.4")) == []
    # 10:30 same day: boundary 1.5%, a +1.6% move breaks ABOVE -> BUY.
    sig = _only(strat.on_bar(_bar(17, 10, 30, open_="101.6", close="101.6")))
    assert sig.side is Side.BUY and sig.reason == "noise-area break"


def test_downside_break_enters_short() -> None:
    strat = NiftyNoiseBreakout(_cfg())
    _warmup_days(strat, 15, 16)
    assert strat.on_bar(_bar(17, 9, 16, open_="100", close="100")) == []
    sig = _only(strat.on_bar(_bar(17, 10, 0, open_="98.4", close="98.4")))  # -1.6% < -1.5%
    assert sig.side is Side.SELL


def test_one_entry_per_day_and_eod_flat() -> None:
    strat = NiftyNoiseBreakout(_cfg())
    _warmup_days(strat, 15, 16)
    strat.on_bar(_bar(17, 9, 16, open_="100", close="100"))
    entry = _only(strat.on_bar(_bar(17, 10, 0, open_="101.6", close="101.6")))
    assert entry.side is Side.BUY
    # Another (bigger) break the same day adds NOTHING — one entry per day.
    assert strat.on_bar(_bar(17, 10, 30, open_="103", close="103")) == []
    # The first close >= 15:05 IST flattens (SELL, score=None — the close convention).
    exit_ = _only(strat.on_bar(_bar(17, 15, 5, open_="102", close="102")))
    assert exit_.side is Side.SELL and exit_.reason == "EOD flat" and exit_.score is None
    # ... and nothing re-enters later that day.
    assert strat.on_bar(_bar(17, 15, 6, open_="110", close="110")) == []


def test_fhh_conditioning_gates_direction_and_early_checks() -> None:
    strat = NiftyNoiseBreakout(_cfg(conditioning=1))
    _warmup_days(strat, 15, 16)
    # Day 3 under fhh: opener, then a NEGATIVE first half hour (09:45 close 99 < open).
    assert strat.on_bar(_bar(17, 9, 16, open_="100", close="100")) == []
    assert strat.on_bar(_bar(17, 9, 45, open_="99", close="99")) == []
    # A +1.6% up-break at 10:00 DISAGREES with the negative fhh sign — blocked.
    assert strat.on_bar(_bar(17, 10, 0, open_="101.6", close="101.6")) == []
    # A -1.6% down-break at 10:30 AGREES — enters short.
    sig = _only(strat.on_bar(_bar(17, 10, 30, open_="98.4", close="98.4")))
    assert sig.side is Side.SELL


def test_fhh_conditioning_blocks_checks_before_the_first_half_hour_ends() -> None:
    # lookback=2 with a 09:30 check history built over two days, then a day-3 09:30
    # break: under conditioning=1 the fhh sign is UNKNOWN at 09:30 — no entry.
    strat = NiftyNoiseBreakout(_cfg(conditioning=1))
    for day, close in zip((15, 16), ("101", "102"), strict=True):
        strat.on_bar(_bar(day, 9, 16, open_="100", close="100"))
        strat.on_bar(_bar(day, 9, 30, open_=close, close=close))
    strat.on_bar(_bar(17, 9, 16, open_="100", close="100"))
    assert strat.on_bar(_bar(17, 9, 30, open_="101.6", close="101.6")) == []
    # The unconditioned twin DOES enter on the identical tape.
    plain = NiftyNoiseBreakout(_cfg())
    for day, close in zip((15, 16), ("101", "102"), strict=True):
        plain.on_bar(_bar(day, 9, 16, open_="100", close="100"))
        plain.on_bar(_bar(day, 9, 30, open_=close, close=close))
    plain.on_bar(_bar(17, 9, 16, open_="100", close="100"))
    assert _only(plain.on_bar(_bar(17, 9, 30, open_="101.6", close="101.6"))).side is Side.BUY


def test_gap_day_flatten_on_rollover() -> None:
    strat = NiftyNoiseBreakout(_cfg())
    _warmup_days(strat, 15, 16)
    strat.on_bar(_bar(17, 9, 16, open_="100", close="100"))
    assert _only(strat.on_bar(_bar(17, 10, 0, open_="101.6", close="101.6"))).side is Side.BUY
    # The tape dies before 15:05 (no EOD-flat bar). The NEXT day's first bar flattens.
    sig = _only(strat.on_bar(_bar(18, 9, 16, open_="100", close="100")))
    assert sig.side is Side.SELL and "gap-day flatten" in (sig.reason or "")


def test_config_validation_fails_fast() -> None:
    with pytest.raises(ValueError, match="conditioning"):
        NiftyNoiseBreakout(_cfg(conditioning=2))
    with pytest.raises(ValueError, match="lookback_days"):
        NiftyNoiseBreakout(_cfg(lookback_days=1))
    with pytest.raises(ValueError, match="fhh_end"):
        NiftyNoiseBreakout(_cfg(fhh_end="15:06", flat_time="15:05"))


def test_registry_and_template_build_the_same_strategy() -> None:
    from alpha_core.research.f1_templates import F1_TEMPLATES
    from alpha_core.strategy.registry import build_strategy

    built = build_strategy("nifty_noise_breakout")
    assert isinstance(built, NiftyNoiseBreakout)
    template = F1_TEMPLATES["nifty_noise_breakout"]
    via_template = template.build({"lookback_days": Decimal("90"), "conditioning": 1})
    assert isinstance(via_template, NiftyNoiseBreakout)
