"""NGE-family last-half-hour momentum strategy tests (the Baltussen form)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from alpha_core.core.enums import AssetClass, Side, Venue
from alpha_core.core.models import Bar, Signal
from alpha_core.strategy.examples.nifty_lhh import NiftyLhhMomentum, NiftyLhhMomentumConfig

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = "NSE:NIFTY 50"
ONE_MIN = timedelta(minutes=1)


def _bar(day: int, hh: int, mm: int, *, open_: str, close: str) -> Bar:
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


def _only(signals: Sequence[Signal]) -> Signal:
    (sig,) = signals
    return sig


def _cfg(**over: object) -> NiftyLhhMomentumConfig:
    return NiftyLhhMomentumConfig.model_validate(over)


def test_signal_reads_the_day_so_far_direction_at_the_signal_close() -> None:
    strat = NiftyLhhMomentum(_cfg())
    assert strat.on_bar(_bar(17, 9, 16, open_="100", close="100")) == []
    assert strat.on_bar(_bar(17, 12, 0, open_="103", close="103")) == []  # before signal_time
    sig = _only(strat.on_bar(_bar(17, 14, 35, open_="102", close="102")))
    assert sig.side is Side.BUY and sig.reason == "last-half-hour momentum"
    # one signal read per day — later bars before flat_time add nothing
    assert strat.on_bar(_bar(17, 14, 40, open_="90", close="90")) == []
    exit_ = _only(strat.on_bar(_bar(17, 15, 5, open_="101", close="101")))
    assert exit_.side is Side.SELL and exit_.score is None


def test_down_day_enters_short_and_flat_day_stands_down() -> None:
    down = NiftyLhhMomentum(_cfg())
    down.on_bar(_bar(17, 9, 16, open_="100", close="100"))
    assert _only(down.on_bar(_bar(17, 14, 35, open_="97", close="97"))).side is Side.SELL
    flat = NiftyLhhMomentum(_cfg())
    flat.on_bar(_bar(17, 9, 16, open_="100", close="100"))
    assert flat.on_bar(_bar(17, 14, 35, open_="100", close="100")) == []  # no direction
    # ... and the read is SPENT: a later mover cannot re-trigger the day
    assert flat.on_bar(_bar(17, 14, 36, open_="104", close="104")) == []


def test_conditioning_gates_on_the_injected_short_gamma_map() -> None:
    signs = {"2026-06-17": -1, "2026-06-18": 1}
    strat = NiftyLhhMomentum(_cfg(conditioning=1), nge_sign_for_day=signs)
    # short-gamma day: trades
    strat.on_bar(_bar(17, 9, 16, open_="100", close="100"))
    assert _only(strat.on_bar(_bar(17, 14, 35, open_="102", close="102"))).side is Side.BUY
    strat.on_bar(_bar(17, 15, 5, open_="102", close="102"))  # flat
    # long-gamma day: stands down
    strat.on_bar(_bar(18, 9, 16, open_="100", close="100"))
    assert strat.on_bar(_bar(18, 14, 35, open_="102", close="102")) == []
    # unknown day (missing key): stands down — no data = no signal
    strat.on_bar(_bar(19, 9, 16, open_="100", close="100"))
    assert strat.on_bar(_bar(19, 14, 35, open_="102", close="102")) == []


def test_unconditioned_ignores_the_map_entirely() -> None:
    strat = NiftyLhhMomentum(_cfg(conditioning=0), nge_sign_for_day={"2026-06-17": 1})
    strat.on_bar(_bar(17, 9, 16, open_="100", close="100"))
    assert _only(strat.on_bar(_bar(17, 14, 35, open_="102", close="102"))).side is Side.BUY


def test_gap_day_resets_silently_and_config_validates() -> None:
    strat = NiftyLhhMomentum(_cfg())
    strat.on_bar(_bar(17, 9, 16, open_="100", close="100"))
    assert _only(strat.on_bar(_bar(17, 14, 35, open_="102", close="102"))).side is Side.BUY
    # tape dies before 15:05; next day's first bar: NO inverse emit (MIS contract)
    assert strat.on_bar(_bar(18, 9, 16, open_="100", close="100")) == []
    with pytest.raises(ValueError, match="conditioning"):
        NiftyLhhMomentum(_cfg(conditioning=2))
    with pytest.raises(ValueError, match="requires an injected"):
        NiftyLhhMomentum(_cfg(conditioning=1))
    with pytest.raises(ValueError, match="signal_time"):
        NiftyLhhMomentum(_cfg(signal_time="15:06", flat_time="15:05"))


def test_registry_and_template_factory_build() -> None:
    from alpha_core.research.nge_templates import nge_templates
    from alpha_core.strategy.registry import build_strategy

    assert isinstance(build_strategy("nifty_lhh_momentum"), NiftyLhhMomentum)
    templates = nge_templates({"2026-06-17": -1})
    conditioned = templates["nifty_lhh_momentum"].build({"conditioning": 1})
    assert isinstance(conditioned, NiftyLhhMomentum)
