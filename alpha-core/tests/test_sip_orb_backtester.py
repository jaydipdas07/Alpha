"""G1 SIP-ORB portfolio fold tests — ORB direction/trigger, one-bar deferrals, VWAP
trail, square-off, RVOL gate + warmup, top-k ranking, pre-open exclusion, costs,
disjoint roots."""

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
from alpha_core.research.sip_orb_backtester import (
    SipOrbBacktester,
    SipOrbConfig,
    _SipOrbSpec,
    build_sip_orb_backtesters,
)
from alpha_core.research.strategist import StrategyProposal

_IST = timedelta(hours=5, minutes=30)
_BUY, _SELL = equity_intraday_cost_sides()
_COSTS = _BUY + _SELL


def _proposal(rvol: str = "2", top_k: int = 5) -> StrategyProposal:
    return StrategyProposal(
        template="sip_orb",
        params={"rvol_threshold": Decimal(rvol), "top_k": Decimal(top_k)},
        market=AssetClass.EQUITY,
        window="nse-sip-orb",
        trial_index=1,
        fingerprint="t",
    )


def _bar(symbol: str, d: date, hh: int, mm: int, o: str, h: str, lo: str, c: str, v: str) -> Bar:
    start_ist = datetime.combine(d, time(hh, mm), tzinfo=UTC)  # treat as IST wall, shift below
    return Bar(
        symbol=symbol,
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        start=start_ist - _IST,  # store keeps UTC
        interval=timedelta(seconds=60),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=Decimal(v),
    )


def _sessions(n: int, start: date = date(2026, 1, 5)) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _quiet_day(symbol: str, d: date, vol_per_bar: str = "20") -> list[Bar]:
    """Flat 100 session: 5 ORB bars + a few post bars + the 15:25 square-off bar.

    first-candle body is UP (100 -> 100.5) so direction exists, but no post-ORB close
    ever exceeds orb_high (101) — never triggers."""
    bars = [_bar(symbol, d, 9, 15, "100", "101", "99.5", "100.5", vol_per_bar)]
    bars += [
        _bar(symbol, d, 9, 15 + i, "100.5", "100.9", "100.1", "100.5", vol_per_bar)
        for i in range(1, 5)
    ]
    for hh, mm in ((9, 20), (9, 21), (10, 0), (12, 0), (15, 0)):
        bars.append(_bar(symbol, d, hh, mm, "100.5", "100.9", "100.1", "100.5", vol_per_bar))
    bars.append(_bar(symbol, d, 15, 25, "100.5", "100.9", "100.1", "100.5", vol_per_bar))
    return bars


def _breakout_day(
    symbol: str,
    d: date,
    *,
    spike_vol_per_bar: str = "60",
    up: bool = True,
    end_price: str = "105",
) -> list[Bar]:
    """First candle with a body; a post-ORB break; drift to ``end_price`` at 15:25.

    Long shape: ORB high 101.5; trigger at 09:21 (close 102); entry deferred to 09:22
    (close 102.5); closes rise monotonically (never below the running VWAP -> no trail
    exit); square-off at 15:25 close ``end_price``.
    Short shape mirrors it below 100 (ORB low 98.5, trigger 98, entry 97.5)."""
    v = spike_vol_per_bar
    if up:
        bars = [_bar(symbol, d, 9, 15, "100", "101.5", "99.5", "101", v)]
        bars += [_bar(symbol, d, 9, 15 + i, "101", "101.4", "100.6", "101", v) for i in range(1, 5)]
        seq = (
            ("9:20", "101"),
            ("9:21", "102"),
            ("9:22", "102.5"),
            ("9:23", "103"),
            ("10:00", "103.5"),
            ("12:00", "104"),
        )
        for hhmm, c in seq:
            hh, mm = (int(x) for x in hhmm.split(":"))
            bars.append(_bar(symbol, d, hh, mm, c, c, c, c, "10"))
        bars.append(_bar(symbol, d, 15, 25, end_price, end_price, end_price, end_price, "10"))
    else:
        bars = [_bar(symbol, d, 9, 15, "100", "100.5", "98.5", "99", v)]
        bars += [_bar(symbol, d, 9, 15 + i, "99", "99.4", "98.6", "99", v) for i in range(1, 5)]
        seq = (
            ("9:20", "99"),
            ("9:21", "98"),
            ("9:22", "97.5"),
            ("9:23", "97"),
            ("10:00", "96.5"),
            ("12:00", "96"),
        )
        for hhmm, c in seq:
            hh, mm = (int(x) for x in hhmm.split(":"))
            bars.append(_bar(symbol, d, hh, mm, c, c, c, c, "10"))
        bars.append(_bar(symbol, d, 15, 25, end_price, end_price, end_price, end_price, "10"))
    return bars


def _panel_reader(panel: dict[str, list[Bar]]) -> Callable[[str], list[Bar]]:
    return lambda s: panel.get(s, [])


def _warmup_panel(symbol: str, sessions: list[date], n_warm: int) -> list[Bar]:
    bars: list[Bar] = []
    for d in sessions[:n_warm]:
        bars.extend(_quiet_day(symbol, d))
    return bars


class TestFoldMechanics:
    def test_long_breakout_books_exact_pnl(self) -> None:
        sessions = _sessions(15)
        bars = _warmup_panel("NSE:AAA", sessions, 14)
        bars += _breakout_day("NSE:AAA", sessions[14], spike_vol_per_bar="60")  # RVOL = 3
        bt = SipOrbBacktester(_panel_reader({"NSE:AAA": bars}), ["NSE:AAA"])
        marks = np.asarray(bt.run(_proposal()))
        assert len(marks) == 15
        expected = ((105.0 / 102.5 - 1.0) - _COSTS) / 5  # entry 09:22 close, sq-off 105
        assert marks[14] == pytest.approx(expected)
        assert marks[:14].sum() == 0.0

    def test_short_breakout_from_down_first_candle(self) -> None:
        sessions = _sessions(15)
        bars = _warmup_panel("NSE:AAA", sessions, 14)
        bars += _breakout_day(
            "NSE:AAA", sessions[14], spike_vol_per_bar="60", up=False, end_price="95"
        )
        bt = SipOrbBacktester(_panel_reader({"NSE:AAA": bars}), ["NSE:AAA"])
        marks = np.asarray(bt.run(_proposal()))
        expected = (-1.0 * (95.0 / 97.5 - 1.0) - _COSTS) / 5  # entry 97.5, cover 95
        assert marks[14] == pytest.approx(expected)

    def test_vwap_trail_exits_before_square_off(self) -> None:
        """Post-entry dip below the running VWAP exits (one-bar deferred) — the later
        rally to 110 must NOT be captured."""
        sessions = _sessions(15)
        d = sessions[14]
        bars = _warmup_panel("NSE:AAA", sessions, 14)
        day = [_bar("NSE:AAA", d, 9, 15, "100", "101.5", "99.5", "101", "60")]
        day += [
            _bar("NSE:AAA", d, 9, 15 + i, "101", "101.4", "100.6", "101", "60") for i in range(1, 5)
        ]
        for hhmm, c in (
            ("9:20", "101"),
            ("9:21", "102"),  # trigger (> 101.5)
            ("9:22", "102.5"),  # entry (deferred)
            ("9:23", "103"),  # above VWAP: no trail
            ("9:24", "98"),  # below VWAP: trail trigger
            ("9:25", "97"),  # exit (deferred)
            ("10:00", "108"),
        ):
            hh, mm = (int(x) for x in hhmm.split(":"))
            day.append(_bar("NSE:AAA", d, hh, mm, c, c, c, c, "10"))
        day.append(_bar("NSE:AAA", d, 15, 25, "110", "110", "110", "110", "10"))
        bt = SipOrbBacktester(_panel_reader({"NSE:AAA": bars + day}), ["NSE:AAA"])
        marks = np.asarray(bt.run(_proposal()))
        expected = ((97.0 / 102.5 - 1.0) - _COSTS) / 5
        assert marks[14] == pytest.approx(expected)

    def test_no_breakout_means_flat_session(self) -> None:
        sessions = _sessions(15)
        bars = _warmup_panel("NSE:AAA", sessions, 14)
        bars += _quiet_day("NSE:AAA", sessions[14], vol_per_bar="60")  # RVOL 3, no trigger
        bt = SipOrbBacktester(_panel_reader({"NSE:AAA": bars}), ["NSE:AAA"])
        assert np.asarray(bt.run(_proposal())).sum() == 0.0

    def test_doji_first_candle_never_trades(self) -> None:
        sessions = _sessions(15)
        d = sessions[14]
        bars = _warmup_panel("NSE:AAA", sessions, 14)
        day = [_bar("NSE:AAA", d, 9, 15, "100", "101.5", "99.5", "100", "60")]  # o == c
        day += [
            _bar("NSE:AAA", d, 9, 15 + i, "100", "101", "99.6", "100", "60") for i in range(1, 5)
        ]
        day.append(_bar("NSE:AAA", d, 9, 21, "102", "102", "102", "102", "10"))
        day.append(_bar("NSE:AAA", d, 9, 22, "103", "103", "103", "103", "10"))
        day.append(_bar("NSE:AAA", d, 15, 25, "104", "104", "104", "104", "10"))
        bt = SipOrbBacktester(_panel_reader({"NSE:AAA": bars + day}), ["NSE:AAA"])
        assert np.asarray(bt.run(_proposal())).sum() == 0.0

    def test_pre_open_prints_excluded_from_orb(self) -> None:
        """A 09:08 pre-open print with a huge range must not widen the opening range
        (the breakout at 102 still triggers)."""
        sessions = _sessions(15)
        d = sessions[14]
        bars = _warmup_panel("NSE:AAA", sessions, 14)
        day = [_bar("NSE:AAA", d, 9, 8, "90", "120", "80", "100", "999")]  # pre-open junk
        day += _breakout_day("NSE:AAA", d, spike_vol_per_bar="60")
        bt = SipOrbBacktester(_panel_reader({"NSE:AAA": bars + day}), ["NSE:AAA"])
        marks = np.asarray(bt.run(_proposal()))
        expected = ((105.0 / 102.5 - 1.0) - _COSTS) / 5
        assert marks[14] == pytest.approx(expected)


class TestSelection:
    def test_rvol_below_threshold_blocks(self) -> None:
        sessions = _sessions(15)
        bars = _warmup_panel("NSE:AAA", sessions, 14)
        bars += _breakout_day("NSE:AAA", sessions[14], spike_vol_per_bar="30")  # RVOL 1.5 < 2
        bt = SipOrbBacktester(_panel_reader({"NSE:AAA": bars}), ["NSE:AAA"])
        assert np.asarray(bt.run(_proposal())).sum() == 0.0

    def test_warmup_floor_blocks_at_exactly_thirteen_priors(self) -> None:
        """13 priors — ONE session short of the 14-session floor — must not trade (the
        #197-review MAJOR: the boundary itself is the pin, not a distant count)."""
        sessions = _sessions(14)
        bars = _warmup_panel("NSE:AAA", sessions, 13)
        bars += _breakout_day("NSE:AAA", sessions[13], spike_vol_per_bar="60")
        bt = SipOrbBacktester(_panel_reader({"NSE:AAA": bars}), ["NSE:AAA"])
        assert np.asarray(bt.run(_proposal())).sum() == 0.0

    def test_rvol_median_excludes_the_current_day(self) -> None:
        """Heterogeneous history [100 x7, 300 x7] (median 200) + a 400 spike day: RVOL is
        exactly 2.0 and MUST trade at threshold 2. A fold that appends the day's own
        volume before its check sees median 300 (RVOL 1.33 -> blocked), and a shortened
        median window sees 300 too — this fixture kills both mutants (#197 review)."""
        sessions = _sessions(15)
        d = sessions[14]
        bars: list[Bar] = []
        for i, s in enumerate(sessions[:14]):
            bars.extend(_quiet_day("NSE:AAA", s, vol_per_bar="20" if i < 7 else "60"))
        bars += _breakout_day("NSE:AAA", d, spike_vol_per_bar="80")  # first5 = 400
        bt = SipOrbBacktester(_panel_reader({"NSE:AAA": bars}), ["NSE:AAA"])
        marks = np.asarray(bt.run(_proposal()))
        expected = ((105.0 / 102.5 - 1.0) - _COSTS) / 5
        assert marks[14] == pytest.approx(expected)

    def test_top_k_takes_highest_rvol_only(self) -> None:
        """Three qualifying names, top_k=2: only the two highest-RVOL trade."""
        sessions = _sessions(15)
        d = sessions[14]
        panel: dict[str, list[Bar]] = {}
        # RVOL 4 / 3 / 2.5 via spike volumes 80/60/50 over a quiet-median 20
        for sym, spike, end in (
            ("NSE:AAA", "80", "105"),
            ("NSE:BBB", "60", "104"),
            ("NSE:CCC", "50", "103"),
        ):
            bars = _warmup_panel(sym, sessions, 14)
            bars += _breakout_day(sym, d, spike_vol_per_bar=spike, end_price=end)
            panel[sym] = bars
        bt = SipOrbBacktester(_panel_reader(panel), list(panel))
        marks = np.asarray(bt.run(_proposal(top_k=2)))
        pnl_a = (105.0 / 102.5 - 1.0) - _COSTS
        pnl_b = (104.0 / 102.5 - 1.0) - _COSTS
        assert marks[14] == pytest.approx((pnl_a + pnl_b) / 2)  # CCC excluded

    def test_missing_symbol_is_out_of_cross_section(self) -> None:
        sessions = _sessions(15)
        bars = _warmup_panel("NSE:AAA", sessions, 14)
        bars += _breakout_day("NSE:AAA", sessions[14], spike_vol_per_bar="60")
        bt = SipOrbBacktester(
            _panel_reader({"NSE:AAA": bars}), ["NSE:AAA", "NSE:GHOST"]
        )  # GHOST un-ingested
        marks = np.asarray(bt.run(_proposal()))
        assert marks[14] == pytest.approx(((105.0 / 102.5 - 1.0) - _COSTS) / 5)


class TestSpecAndBuilder:
    def test_rvol_threshold_bounds(self) -> None:
        with pytest.raises(ValueError, match="rvol_threshold"):
            _SipOrbSpec(SipOrbConfig(rvol_threshold=Decimal("1")))

    def test_top_k_bounds(self) -> None:
        with pytest.raises(ValueError, match="top_k"):
            _SipOrbSpec(SipOrbConfig(top_k=1))

    def test_builder_asserts_disjoint_roots(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            build_sip_orb_backtesters(
                research_store=BarStore(tmp_path / "a"),
                holdout_store=HoldoutStore(tmp_path / "a"),
                universe=["NSE:AAA"],
            )
