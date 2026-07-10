"""G3 square-off unwind fold tests — fade direction, one-bar deferral, decision
staleness window, exit at square-off, threshold + |r| ranking, pre-open exclusion,
costs, disjoint roots."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.holdout import HoldoutStore
from alpha_core.data.store import BarStore
from alpha_core.research.cost_scenarios import equity_intraday_cost_sides
from alpha_core.research.squareoff_backtester import (
    SquareoffBacktester,
    SquareoffUnwindConfig,
    _SquareoffSpec,
    build_squareoff_backtesters,
)
from alpha_core.research.strategist import StrategyProposal

_IST = timedelta(hours=5, minutes=30)
_BUY, _SELL = equity_intraday_cost_sides()
_COSTS = _BUY + _SELL
D = date(2026, 1, 5)  # a Monday


def _proposal(threshold: str = "1", top_k: int = 5) -> StrategyProposal:
    return StrategyProposal(
        template="squareoff_unwind",
        params={"threshold_pct": Decimal(threshold), "top_k": Decimal(top_k)},
        market=AssetClass.EQUITY,
        window="nse-squareoff",
        trial_index=1,
        fingerprint="t",
    )


def _bar(symbol: str, d: date, hh: int, mm: int, o: str, c: str, v: str = "10") -> Bar:
    px_o, px_c = Decimal(o), Decimal(c)
    return Bar(
        symbol=symbol,
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        start=datetime.combine(d, time(hh, mm), tzinfo=UTC) - _IST,
        interval=timedelta(seconds=60),
        open=px_o,
        high=max(px_o, px_c),
        low=min(px_o, px_c),
        close=px_c,
        volume=Decimal(v),
    )


def _day(
    symbol: str,
    *,
    d: date = D,
    open_px: str = "100",
    dec_px: str = "102",
    entry_px: str = "101.8",
    sq_px: str = "101",
    with_decision_bar: bool = True,
    pre_open_junk: bool = False,
) -> list[Bar]:
    """One session: open bar, a mid-day bar, the 15:10 decision bar, the 15:11 entry
    bar, a 15:12 bar, and the 15:25 square-off bar."""
    bars: list[Bar] = []
    if pre_open_junk:
        bars.append(_bar(symbol, d, 9, 8, "50", "50"))  # pre-open print: must be ignored
    bars.append(_bar(symbol, d, 9, 15, open_px, open_px))
    bars.append(_bar(symbol, d, 12, 0, open_px, dec_px))
    if with_decision_bar:
        bars.append(_bar(symbol, d, 15, 10, dec_px, dec_px))
    bars.append(_bar(symbol, d, 15, 11, entry_px, entry_px))
    bars.append(_bar(symbol, d, 15, 12, entry_px, entry_px))
    bars.append(_bar(symbol, d, 15, 25, sq_px, sq_px))
    return bars


def _reader(panel: dict[str, list[Bar]]) -> Callable[[str], list[Bar]]:
    return lambda s: panel.get(s, [])


class TestFoldMechanics:
    def test_up_day_fades_short_with_exact_pnl(self) -> None:
        """+2% day at 15:10: SHORT at the deferred 15:11 close (101.8, NOT the 102
        decision print — a zero-deferral mutant books a different pnl), cover at the
        15:25 square-off close (101)."""
        bt = SquareoffBacktester(_reader({"NSE:AAA": _day("NSE:AAA")}), ["NSE:AAA"])
        marks = np.asarray(bt.run(_proposal()))
        expected = (-1.0 * (101.0 / 101.8 - 1.0) - _COSTS) / 5
        assert marks.sum() == pytest.approx(expected)

    def test_down_day_fades_long(self) -> None:
        day = _day("NSE:AAA", dec_px="98", entry_px="98.2", sq_px="99")
        bt = SquareoffBacktester(_reader({"NSE:AAA": day}), ["NSE:AAA"])
        marks = np.asarray(bt.run(_proposal()))
        expected = ((99.0 / 98.2 - 1.0) - _COSTS) / 5
        assert marks.sum() == pytest.approx(expected)

    def test_below_threshold_no_trade(self) -> None:
        day = _day("NSE:AAA", dec_px="100.5")  # +0.5% < 1%
        bt = SquareoffBacktester(_reader({"NSE:AAA": day}), ["NSE:AAA"])
        assert np.asarray(bt.run(_proposal())).sum() == 0.0

    def test_missing_decision_window_no_trade(self) -> None:
        """No bar starts inside [15:10, 15:15): the first bar at/after 15:10 is the
        15:25 square-off bar itself => stale decision => no trade, even though the day
        moved 2% (staleness, never interpolation)."""
        day = [
            _bar("NSE:AAA", D, 9, 15, "100", "100"),
            _bar("NSE:AAA", D, 12, 0, "100", "102"),
            _bar("NSE:AAA", D, 15, 25, "101", "101"),
        ]
        bt = SquareoffBacktester(_reader({"NSE:AAA": day}), ["NSE:AAA"])
        assert np.asarray(bt.run(_proposal())).sum() == 0.0

    def test_pre_open_print_does_not_set_day_open(self) -> None:
        """A 09:08 pre-open print at 50 must not become the session open (it would make
        every day a fake +100% mover)."""
        day = _day("NSE:AAA", pre_open_junk=True, dec_px="100.5")  # real move +0.5% < 1%
        bt = SquareoffBacktester(_reader({"NSE:AAA": day}), ["NSE:AAA"])
        assert np.asarray(bt.run(_proposal())).sum() == 0.0

    def test_zero_session_marks_span_the_calendar(self) -> None:
        """Two sessions, only one trades: the marks series has one slot per session."""
        d2 = date(2026, 1, 6)
        bars = _day("NSE:AAA") + _day("NSE:AAA", d=d2, dec_px="100.2")
        bt = SquareoffBacktester(_reader({"NSE:AAA": bars}), ["NSE:AAA"])
        marks = np.asarray(bt.run(_proposal()))
        assert len(marks) == 2
        assert marks[0] != 0.0
        assert marks[1] == 0.0


class TestSelection:
    def test_top_k_ranks_by_abs_move(self) -> None:
        """|r| = 3% / 2.5% / 1.5%, top_k=2: only the first two trade (one short, one
        long — the sign must follow each name's own move)."""
        panel = {
            "NSE:AAA": _day("NSE:AAA", dec_px="103", entry_px="102.8", sq_px="102"),  # +3% short
            "NSE:BBB": _day("NSE:BBB", dec_px="97.5", entry_px="97.7", sq_px="98.5"),  # -2.5% long
            "NSE:CCC": _day("NSE:CCC", dec_px="101.5", entry_px="101.4", sq_px="101"),  # +1.5%
        }
        bt = SquareoffBacktester(_reader(panel), list(panel))
        marks = np.asarray(bt.run(_proposal(top_k=2)))
        pnl_a = -1.0 * (102.0 / 102.8 - 1.0) - _COSTS
        pnl_b = (98.5 / 97.7 - 1.0) - _COSTS
        assert marks.sum() == pytest.approx((pnl_a + pnl_b) / 2)  # CCC excluded

    def test_missing_symbol_is_out_of_cross_section(self) -> None:
        bt = SquareoffBacktester(_reader({"NSE:AAA": _day("NSE:AAA")}), ["NSE:AAA", "NSE:GHOST"])
        marks = np.asarray(bt.run(_proposal()))
        expected = (-1.0 * (101.0 / 101.8 - 1.0) - _COSTS) / 5
        assert marks.sum() == pytest.approx(expected)


class TestSpecAndBuilder:
    def test_threshold_bounds(self) -> None:
        with pytest.raises(ValueError, match="threshold_pct"):
            _SquareoffSpec(SquareoffUnwindConfig(threshold_pct=Decimal("0")))

    def test_top_k_bounds(self) -> None:
        with pytest.raises(ValueError, match="top_k"):
            _SquareoffSpec(SquareoffUnwindConfig(top_k=1))

    def test_builder_asserts_disjoint_roots(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            build_squareoff_backtesters(
                research_store=BarStore(tmp_path / "a"),
                holdout_store=HoldoutStore(tmp_path / "a"),
                universe=["NSE:AAA"],
            )
