"""G4 max-pain drift fold tests — the pain argmin, prior-day-only OI (look-ahead kill),
direction toward max pain, distance gate, exit times, calendar clipping, costs,
disjoint roots on both pairs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from alpha_core.core.enums import AssetClass, OptionRight, Venue
from alpha_core.core.models import Bar
from alpha_core.data.holdout import HoldoutStore
from alpha_core.data.options_store import OptionQuote, OptionsStore
from alpha_core.data.store import BarStore
from alpha_core.research.cost_scenarios import index_future_cost_sides
from alpha_core.research.max_pain_backtester import (
    MaxPainBacktester,
    MaxPainDriftConfig,
    _MaxPainSpec,
    build_max_pain_backtesters,
    max_pain_strike,
)
from alpha_core.research.strategist import StrategyProposal

_IST = timedelta(hours=5, minutes=30)
_BUY, _SELL = index_future_cost_sides()
_COSTS = _BUY + _SELL
SYM = "NSE:NIFTY 50"
E = date(2026, 1, 6)  # the expiry session (a Tuesday)
E_PRIOR = date(2026, 1, 5)


def _proposal(distance: str = "0.3", exit_minute: int = 870) -> StrategyProposal:
    return StrategyProposal(
        template="max_pain_drift",
        params={"distance_pct": Decimal(distance), "exit_minute": Decimal(exit_minute)},
        market=AssetClass.EQUITY,
        window="nifty-max-pain",
        trial_index=1,
        fingerprint="t",
    )


def _bar(d: date, hh: int, mm: int, c: str) -> Bar:
    px = Decimal(c)
    return Bar(
        symbol=SYM,
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        start=datetime.combine(d, time(hh, mm), tzinfo=UTC) - _IST,
        interval=timedelta(seconds=60),
        open=px,
        high=px,
        low=px,
        close=px,
        volume=Decimal("0"),
    )


def _quote(trade: date, expiry: date, strike: str, right: OptionRight, oi: int) -> OptionQuote:
    return OptionQuote(
        underlying="NIFTY",
        venue=Venue.NSE,
        trade_date=datetime.combine(trade, time(0, 0), tzinfo=UTC),
        expiry=datetime.combine(expiry, time(0, 0), tzinfo=UTC),
        strike=Decimal(strike),
        right=right,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        settle=Decimal("0"),
        volume_contracts=1,
        open_interest=oi,
        change_in_oi=0,
    )


def _chain_pinning_100(trade: date, expiry: date) -> list[OptionQuote]:
    """Prior-day OI whose max pain is exactly 100: heavy calls at 105 and puts at 95
    make any settlement away from 100 costly to writers."""
    return [
        _quote(trade, expiry, "95", OptionRight.PUT, 100),
        _quote(trade, expiry, "100", OptionRight.CALL, 50),
        _quote(trade, expiry, "100", OptionRight.PUT, 50),
        _quote(trade, expiry, "105", OptionRight.CALL, 100),
    ]


def _expiry_day_bars(
    *,
    d: date = E,
    spot: str = "101",
    entry: str = "100.8",
    exit_1430: str = "100.1",
    exit_1515: str = "100.3",
) -> list[Bar]:
    """Decision window closes at ``spot``; entry bar 09:20; two exit-time bars; 15:25."""
    return [
        _bar(d, 9, 15, "100.5"),
        _bar(d, 9, 19, spot),  # the LAST bar inside [09:15, 09:20) sets the decision
        _bar(d, 9, 20, entry),
        _bar(d, 12, 0, "100.5"),
        _bar(d, 14, 30, exit_1430),
        _bar(d, 15, 15, exit_1515),
        _bar(d, 15, 25, "100.2"),
    ]


def _reader(bars: list[Bar]) -> Callable[[str], list[Bar]]:
    return lambda _s: bars


class TestMaxPainStrike:
    def test_pain_argmin(self) -> None:
        strikes = np.array([95.0, 100.0, 100.0, 105.0])
        is_call = np.array([False, True, False, True])
        oi = np.array([100.0, 50.0, 50.0, 100.0])
        assert max_pain_strike(strikes, is_call, oi) == 100.0

    def test_tie_resolves_to_lowest_strike(self) -> None:
        # symmetric OI: pain equal at both strikes -> the lower one wins
        strikes = np.array([100.0, 110.0])
        is_call = np.array([True, False])
        oi = np.array([10.0, 10.0])
        assert max_pain_strike(strikes, is_call, oi) == 100.0


class TestFold:
    def test_spot_above_pain_shorts_toward_it_exact_pnl(self, tmp_path: Path) -> None:
        """Spot 101 vs max pain 100 (+1% >= 0.3%): SHORT at the 09:20 close (100.8),
        exit at the 14:30 close (100.1) — pnl exact, costs charged."""
        store = OptionsStore(tmp_path / "opt")
        store.write(_chain_pinning_100(E_PRIOR, E))
        bt = MaxPainBacktester(_reader(_expiry_day_bars()), store)
        marks = np.asarray(bt.run(_proposal()))
        expected = -1.0 * (100.1 / 100.8 - 1.0) - _COSTS
        assert marks.sum() == pytest.approx(expected)

    def test_spot_below_pain_longs(self, tmp_path: Path) -> None:
        store = OptionsStore(tmp_path / "opt")
        store.write(_chain_pinning_100(E_PRIOR, E))
        bars = _expiry_day_bars(spot="99", entry="99.2", exit_1430="99.8")
        bt = MaxPainBacktester(_reader(bars), store)
        marks = np.asarray(bt.run(_proposal()))
        expected = (99.8 / 99.2 - 1.0) - _COSTS
        assert marks.sum() == pytest.approx(expected)

    def test_exit_minute_915_uses_the_1515_bar(self, tmp_path: Path) -> None:
        store = OptionsStore(tmp_path / "opt")
        store.write(_chain_pinning_100(E_PRIOR, E))
        bt = MaxPainBacktester(_reader(_expiry_day_bars()), store)
        marks = np.asarray(bt.run(_proposal(exit_minute=915)))
        expected = -1.0 * (100.3 / 100.8 - 1.0) - _COSTS
        assert marks.sum() == pytest.approx(expected)

    def test_below_distance_threshold_no_trade(self, tmp_path: Path) -> None:
        store = OptionsStore(tmp_path / "opt")
        store.write(_chain_pinning_100(E_PRIOR, E))
        bars = _expiry_day_bars(spot="100.2")  # +0.2% < 0.3%
        bt = MaxPainBacktester(_reader(bars), store)
        assert np.asarray(bt.run(_proposal())).sum() == 0.0

    def test_same_day_oi_never_enters_the_pain(self, tmp_path: Path) -> None:
        """A poisoned SAME-DAY chain (trade_date == expiry) that would drag max pain to
        110 must be ignored — the pain uses the PRIOR session only (look-ahead kill).
        With the prior chain pinning 100, the +1% dislocation still trades; a fold that
        consumed same-day OI would see spot 101 BELOW pain 110 and flip the side."""
        store = OptionsStore(tmp_path / "opt")
        store.write(_chain_pinning_100(E_PRIOR, E))
        store.write([_quote(E, E, "110", OptionRight.PUT, 100000)])
        bt = MaxPainBacktester(_reader(_expiry_day_bars()), store)
        marks = np.asarray(bt.run(_proposal()))
        expected = -1.0 * (100.1 / 100.8 - 1.0) - _COSTS  # SHORT, as with the clean chain
        assert marks.sum() == pytest.approx(expected)

    def test_expiry_without_prior_chain_never_anchors_the_era(self, tmp_path: Path) -> None:
        """An expiry with only a SAME-DAY chain is untradeable AND outside the usable
        era: with a second (usable) expiry a week on, the calendar clips to that one
        session only, and no trade books there (below threshold)."""
        store = OptionsStore(tmp_path / "opt")
        d2 = date(2026, 1, 13)  # a second expiry, one week on
        store.write([_quote(E, E, "100", OptionRight.CALL, 10)])  # same-day only
        store.write(_chain_pinning_100(date(2026, 1, 12), d2))
        bars = _expiry_day_bars() + _expiry_day_bars(d=d2, spot="100.1")  # d2: below theta
        bt = MaxPainBacktester(_reader(bars), store)
        marks = np.asarray(bt.run(_proposal()))
        assert len(marks) == 1  # E is NOT a usable expiry: the era is [d2, d2]
        assert marks.sum() == 0.0

    def test_calendar_clipped_to_chain_era(self, tmp_path: Path) -> None:
        """Sessions outside [first usable expiry, last usable expiry] are NOT in the
        marks calendar — a fence gap must not dilute the marks with dead zeros."""
        store = OptionsStore(tmp_path / "opt")
        store.write(_chain_pinning_100(E_PRIOR, E))
        bars = _expiry_day_bars() + _expiry_day_bars(d=date(2026, 3, 2), spot="100")
        bt = MaxPainBacktester(_reader(bars), store)
        marks = np.asarray(bt.run(_proposal()))
        assert len(marks) == 1  # the March session is outside the chain era


class TestSpecAndBuilder:
    def test_distance_bounds(self) -> None:
        with pytest.raises(ValueError, match="distance_pct"):
            _MaxPainSpec(MaxPainDriftConfig(distance_pct=Decimal("0")))

    def test_exit_minute_bounds(self) -> None:
        with pytest.raises(ValueError, match="exit_minute"):
            _MaxPainSpec(MaxPainDriftConfig(exit_minute=100))

    def test_builder_asserts_disjoint_roots_on_both_pairs(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            build_max_pain_backtesters(
                research_store=BarStore(tmp_path / "a"),
                holdout_store=HoldoutStore(tmp_path / "a"),
                research_options=OptionsStore(tmp_path / "o1"),
                holdout_options=OptionsStore(tmp_path / "o2"),
            )
        with pytest.raises(ValueError):
            build_max_pain_backtesters(
                research_store=BarStore(tmp_path / "b1"),
                holdout_store=HoldoutStore(tmp_path / "b2"),
                research_options=OptionsStore(tmp_path / "o"),
                holdout_options=OptionsStore(tmp_path / "o"),
            )
