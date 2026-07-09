"""G6 depth-imbalance fold tests — strict trade-through fills, TTL, persistence,
direction, non-overlap, costs, honest-NaN, disjoint roots."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.depth_store import BANDS, DepthRow, DepthStore
from alpha_core.data.tick_store import TickStore
from alpha_core.research.cost_scenarios import cost_per_side
from alpha_core.research.depth_backtester import (
    DepthImbalanceBacktester,
    DepthImbalanceConfig,
    _DepthImbalanceSpec,
    build_depth_backtesters,
)
from alpha_core.research.strategist import StrategyProposal

T0 = int(datetime(2026, 1, 5, tzinfo=UTC).timestamp())  # minute-aligned
_N = len(BANDS)
_COSTS = cost_per_side("maker") + cost_per_side("taker")


def _proposal(band: str = "0.2", threshold: str = "0.3", hold: int = 15) -> StrategyProposal:
    return StrategyProposal(
        template="depth_imbalance",
        params={
            "band_pct": Decimal(band),
            "threshold": Decimal(threshold),
            "hold_minutes": Decimal(hold),
        },
        market=AssetClass.CRYPTO,
        window="btcusdt-depth-imb",
        trial_index=1,
        fingerprint="t",
    )


def _bar(epoch: int, low: str, high: str, close: str) -> Bar:
    return Bar(
        symbol="BTCUSDT",
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        start=datetime.fromtimestamp(epoch, tz=UTC),
        interval=timedelta(seconds=1),
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1"),
    )


def _snapshot(epoch: int, bid02: float, ask02: float) -> DepthRow:
    vals = [math.nan] * (2 * _N)
    vals[0] = bid02
    vals[_N] = ask02
    return (epoch, *vals)  # type: ignore[return-value]


def _stores(tmp_path: Path) -> tuple[TickStore, DepthStore]:
    return TickStore(tmp_path / "ticks"), DepthStore(tmp_path / "depth")


def _write_tape(
    ticks: TickStore,
    *,
    seconds: int = 2000,
    dip_at: int | None = None,
    dip_low: str = "99.5",
    spike_at: int | None = None,
    spike_high: str = "100.5",
    exit_close: str | None = None,
    exit_from: int | None = None,
) -> None:
    """A flat $100 1s tape; optionally one dip/spike bar and a later close step."""
    bars = []
    for i in range(seconds):
        low, high, close = "100", "100", "100"
        if exit_close is not None and exit_from is not None and i >= exit_from:
            low = high = close = exit_close
        if dip_at is not None and i == dip_at:
            low = dip_low
        if spike_at is not None and i == spike_at:
            high = spike_high
        bars.append(_bar(T0 + i, low, high, close))
    ticks.write_bars(bars, month="2026-01")


def _write_bid_heavy_signal(depth: DepthStore, *, at: tuple[int, ...] = (0, 30)) -> None:
    """|I| = 0.6 bid-heavy (800/200) at the given offsets — arms threshold 0.3/0.5."""
    depth.write_month(
        venue=Venue.BINANCE,
        symbol="BTCUSDT",
        month="2026-01",
        rows=[_snapshot(T0 + s, 800.0, 200.0) for s in at],
    )


class TestFillRule:
    def test_strict_trade_through_fills_at_limit(self, tmp_path: Path) -> None:
        """Trigger at T0+30, arrival T0+32, L=100; dip to 99.5 at T0+40 fills AT 100;
        exit 15m after the fill at the stepped close 101 — pnl is exact."""
        ticks, depth = _stores(tmp_path)
        # exit target = fill(40) + 900 = 940; close steps to 101 well before, at 500
        _write_tape(ticks, dip_at=40, exit_close="101", exit_from=500)
        _write_bid_heavy_signal(depth)
        bt = DepthImbalanceBacktester(ticks, depth)
        marks = np.asarray(bt.run(_proposal()))
        expected = (101.0 / 100.0 - 1.0) - _COSTS
        assert marks.sum() == pytest.approx(expected)
        # the mark lands on the EXIT minute: (T0+940)//60 - T0//60 = 15
        assert marks[15] == pytest.approx(expected)

    def test_touch_never_fills(self, tmp_path: Path) -> None:
        """A bar whose low EQUALS the limit is a touch — queue position unknowable, no
        fill (#184 verbatim)."""
        ticks, depth = _stores(tmp_path)
        _write_tape(ticks, dip_at=40, dip_low="100")  # low == L exactly
        _write_bid_heavy_signal(depth)
        bt = DepthImbalanceBacktester(ticks, depth)
        assert np.asarray(bt.run(_proposal())).sum() == 0.0

    def test_ttl_expiry_means_no_trade(self, tmp_path: Path) -> None:
        """The only trade-through print sits past arrival+TTL (T0+32+60=92): missed."""
        ticks, depth = _stores(tmp_path)
        _write_tape(ticks, dip_at=100)  # 100 > 92
        _write_bid_heavy_signal(depth)
        bt = DepthImbalanceBacktester(ticks, depth)
        assert np.asarray(bt.run(_proposal())).sum() == 0.0

    def test_ask_heavy_sells_and_fills_on_upward_trade_through(self, tmp_path: Path) -> None:
        ticks, depth = _stores(tmp_path)
        # SELL rests at 100; spike to 100.5 at T0+40 fills; exit at 99 => +1% gross
        _write_tape(ticks, spike_at=40, exit_close="99", exit_from=500)
        depth.write_month(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            month="2026-01",
            rows=[_snapshot(T0 + s, 200.0, 800.0) for s in (0, 30)],
        )
        bt = DepthImbalanceBacktester(ticks, depth)
        expected = -1.0 * (99.0 / 100.0 - 1.0) - _COSTS
        assert np.asarray(bt.run(_proposal())).sum() == pytest.approx(expected)

    def test_unfinished_tail_trade_dropped(self, tmp_path: Path) -> None:
        """hold=60m runs past the tape end: the trade is dropped, never fabricated."""
        ticks, depth = _stores(tmp_path)
        _write_tape(ticks, seconds=2000, dip_at=40)  # fill at 40 + 3600 >> 2000
        _write_bid_heavy_signal(depth)
        bt = DepthImbalanceBacktester(ticks, depth)
        assert np.asarray(bt.run(_proposal(hold=60))).sum() == 0.0


class TestSignal:
    def test_single_snapshot_is_not_persistent(self, tmp_path: Path) -> None:
        ticks, depth = _stores(tmp_path)
        _write_tape(ticks, dip_at=40, exit_close="101", exit_from=500)
        # snapshots: armed at 30 only; 0 and 60 are balanced (I=0)
        depth.write_month(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            month="2026-01",
            rows=[
                _snapshot(T0, 500.0, 500.0),
                _snapshot(T0 + 30, 800.0, 200.0),
                _snapshot(T0 + 60, 500.0, 500.0),
            ],
        )
        bt = DepthImbalanceBacktester(ticks, depth)
        assert np.asarray(bt.run(_proposal())).sum() == 0.0

    def test_gap_between_snapshots_breaks_persistence(self, tmp_path: Path) -> None:
        ticks, depth = _stores(tmp_path)
        _write_tape(ticks, dip_at=160, exit_close="101", exit_from=500)
        _write_bid_heavy_signal(depth, at=(0, 120))  # 120s gap > _PERSIST_MAX_GAP_S=90
        bt = DepthImbalanceBacktester(ticks, depth)
        assert np.asarray(bt.run(_proposal())).sum() == 0.0

    def test_sign_flip_breaks_persistence(self, tmp_path: Path) -> None:
        ticks, depth = _stores(tmp_path)
        _write_tape(ticks, dip_at=40, spike_at=41, exit_close="101", exit_from=500)
        depth.write_month(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            month="2026-01",
            rows=[_snapshot(T0, 200.0, 800.0), _snapshot(T0 + 30, 800.0, 200.0)],
        )
        bt = DepthImbalanceBacktester(ticks, depth)
        assert np.asarray(bt.run(_proposal())).sum() == 0.0

    def test_nan_band_is_no_signal(self, tmp_path: Path) -> None:
        ticks, depth = _stores(tmp_path)
        _write_tape(ticks, dip_at=40, exit_close="101", exit_from=500)
        rows = []
        for s in (0, 30):
            vals = [math.nan] * (2 * _N)
            vals[0] = 800.0  # bid published, ask side missing => honest-NaN => no signal
            rows.append((T0 + s, *vals))
        depth.write_month(venue=Venue.BINANCE, symbol="BTCUSDT", month="2026-01", rows=rows)  # type: ignore[arg-type]
        bt = DepthImbalanceBacktester(ticks, depth)
        assert np.asarray(bt.run(_proposal())).sum() == 0.0

    def test_below_threshold_never_arms(self, tmp_path: Path) -> None:
        ticks, depth = _stores(tmp_path)
        _write_tape(ticks, dip_at=40, exit_close="101", exit_from=500)
        # I = 0.2 < 0.3: never armed
        depth.write_month(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            month="2026-01",
            rows=[_snapshot(T0 + s, 600.0, 400.0) for s in (0, 30)],
        )
        bt = DepthImbalanceBacktester(ticks, depth)
        assert np.asarray(bt.run(_proposal())).sum() == 0.0


class TestNonOverlap:
    def test_one_position_at_a_time(self, tmp_path: Path) -> None:
        """Persistent signal every 30s: the resting/holding window must swallow the
        later triggers — exactly one trade books."""
        ticks, depth = _stores(tmp_path)
        _write_tape(ticks, dip_at=40, exit_close="101", exit_from=500)
        _write_bid_heavy_signal(depth, at=(0, 30, 60, 90, 120))
        bt = DepthImbalanceBacktester(ticks, depth)
        marks = np.asarray(bt.run(_proposal()))
        expected = (101.0 / 100.0 - 1.0) - _COSTS
        assert marks.sum() == pytest.approx(expected)  # one trade's pnl, not five
        assert (marks != 0).sum() == 1


class TestSpecAndBuilder:
    def test_band_must_be_archived(self) -> None:
        with pytest.raises(ValueError, match="band_pct"):
            _DepthImbalanceSpec(DepthImbalanceConfig(band_pct=Decimal("0.5")))

    def test_threshold_bounded(self) -> None:
        with pytest.raises(ValueError, match="threshold"):
            _DepthImbalanceSpec(DepthImbalanceConfig(threshold=Decimal("1.5")))

    def test_builder_asserts_disjoint_roots(self, tmp_path: Path) -> None:
        ticks = TickStore(tmp_path / "ticks")
        depth = DepthStore(tmp_path / "depth")
        with pytest.raises(ValueError):
            build_depth_backtesters(
                research_ticks=ticks,
                holdout_ticks=ticks,
                research_depth=depth,
                holdout_depth=DepthStore(tmp_path / "depth2"),
            )
