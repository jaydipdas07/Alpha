"""G7 premium-dislocation fold tests — z-trigger, fade direction, warmup floor,
honest-NaN, wiring into the shared maker fill, disjoint roots.

The execution semantics (decision-priced limit, GTX, strict trade-through, TTL, busy
windows, costs) are pinned end-to-end by ``test_depth_backtester.py`` through the same
``maker_fill.run_post_only_fold`` — these tests pin the premium-specific layer."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.premium_store import PremiumRow, PremiumStore
from alpha_core.data.tick_store import TickStore
from alpha_core.research.cost_scenarios import cost_per_side
from alpha_core.research.premium_backtester import (
    PremiumDislocationBacktester,
    PremiumDislocationConfig,
    _PremiumDislocationSpec,
    build_premium_backtesters,
)
from alpha_core.research.strategist import StrategyProposal

T0 = int(datetime(2026, 1, 5, tzinfo=UTC).timestamp())  # minute-aligned
_COSTS = cost_per_side("maker") + cost_per_side("taker")
_WARM = 760  # premium bars written before the spike (> the 720 warmup floor)


def _proposal(sigma: str = "3", hold: int = 30) -> StrategyProposal:
    return StrategyProposal(
        template="premium_dislocation",
        params={"threshold_sigma": Decimal(sigma), "hold_minutes": Decimal(hold)},
        market=AssetClass.CRYPTO,
        window="btcusdt-prem-disloc",
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


def _stores(tmp_path: Path) -> tuple[TickStore, PremiumStore]:
    return TickStore(tmp_path / "ticks"), PremiumStore(tmp_path / "prem")


def _write_premium(
    prem: PremiumStore, *, spike: float | None = None, spike_nan: bool = False
) -> int:
    """±1bp alternating premium noise for _WARM minutes, then one optional spike bar.

    Returns the spike bar's END epoch (the trigger decision instant)."""
    rows: list[PremiumRow] = []
    for i in range(_WARM):
        v = 0.0001 if i % 2 == 0 else -0.0001
        e = T0 + (i + 1) * 60  # bar END
        rows.append((e, v, v, v, v))
    spike_end = T0 + (_WARM + 1) * 60
    if spike is not None:
        rows.append((spike_end, spike, spike, spike, spike))
    elif spike_nan:
        rows.append((spike_end, math.nan, math.nan, math.nan, math.nan))
    prem.write_month(venue=Venue.BINANCE, symbol="BTCUSDT", month="2026-01", rows=rows)
    return spike_end


def _write_tape(
    ticks: TickStore,
    *,
    start: int,
    seconds: int,
    move_at: int | None = None,
    move: tuple[str, str, str] | None = None,
    exit_close: str | None = None,
    exit_from: int | None = None,
) -> None:
    """A flat $100 1s tape from ``start``; one optional moved bar and a close step."""
    bars = []
    for i in range(seconds):
        low, high, close = "100", "100", "100"
        if exit_close is not None and exit_from is not None and i >= exit_from:
            low = high = close = exit_close
        if move_at is not None and move is not None and i == move_at:
            low, high, close = move
        bars.append(_bar(start + i, low, high, close))
    ticks.write_bars(bars, month="2026-01")


class TestSignal:
    def test_positive_spike_sells_and_books_exact_pnl(self, tmp_path: Path) -> None:
        """A +50bp premium spike vs ±1bp noise (z >> 3) fades SHORT: the resting SELL
        at the decision print (100) fills on the upward trade-through at +40s, exits
        taker 30m later at 99 — pnl is exact."""
        ticks, prem = _stores(tmp_path)
        dec = _write_premium(prem, spike=0.005)
        _write_tape(
            ticks,
            start=dec - 60,
            seconds=2400,
            move_at=100,  # dec is at offset 60 in the tape; arrival 62; through at 100
            move=("100", "100.5", "100"),
            exit_close="99",
            exit_from=500,
        )
        bt = PremiumDislocationBacktester(ticks, prem)
        marks = np.asarray(bt.run(_proposal()))
        expected = -1.0 * (99.0 / 100.0 - 1.0) - _COSTS
        assert marks.sum() == pytest.approx(expected)

    def test_negative_spike_buys(self, tmp_path: Path) -> None:
        ticks, prem = _stores(tmp_path)
        dec = _write_premium(prem, spike=-0.005)
        _write_tape(
            ticks,
            start=dec - 60,
            seconds=2400,
            move_at=100,
            move=("99.5", "100", "100"),  # downward trade-through fills the BUY
            exit_close="101",
            exit_from=500,
        )
        bt = PremiumDislocationBacktester(ticks, prem)
        marks = np.asarray(bt.run(_proposal()))
        expected = (101.0 / 100.0 - 1.0) - _COSTS
        assert marks.sum() == pytest.approx(expected)

    def test_below_threshold_never_triggers(self, tmp_path: Path) -> None:
        """A +2bp bump vs ±1bp noise (z ≈ 2 < 3) must not trigger."""
        ticks, prem = _stores(tmp_path)
        dec = _write_premium(prem, spike=0.0002)
        _write_tape(
            ticks,
            start=dec - 60,
            seconds=2400,
            move_at=100,
            move=("99.5", "100.5", "100"),
            exit_close="99",
            exit_from=500,
        )
        bt = PremiumDislocationBacktester(ticks, prem)
        assert np.asarray(bt.run(_proposal())).sum() == 0.0

    def test_warmup_floor_blocks_early_signal(self, tmp_path: Path) -> None:
        """A spike before 720 valid samples sit in the window must not trigger."""
        ticks, prem = _stores(tmp_path)
        rows: list[PremiumRow] = []
        for i in range(100):  # only 100 warmup samples
            v = 0.0001 if i % 2 == 0 else -0.0001
            rows.append((T0 + (i + 1) * 60, v, v, v, v))
        spike_end = T0 + 101 * 60
        rows.append((spike_end, 0.005, 0.005, 0.005, 0.005))
        prem.write_month(venue=Venue.BINANCE, symbol="BTCUSDT", month="2026-01", rows=rows)
        _write_tape(
            ticks,
            start=spike_end - 60,
            seconds=2400,
            move_at=100,
            move=("99.5", "100.5", "100"),
            exit_close="99",
            exit_from=500,
        )
        bt = PremiumDislocationBacktester(ticks, prem)
        assert np.asarray(bt.run(_proposal())).sum() == 0.0

    def test_warmup_floor_is_exact_at_min_samples(self, tmp_path: Path) -> None:
        """718 warmup samples + spike (nv=719 < 720) is blocked; 719 + spike (nv=720)
        fires — the min-samples floor is exact in both directions."""
        for warm, fires in ((718, False), (719, True)):
            ticks, prem = _stores(tmp_path / f"w{warm}")
            rows: list[PremiumRow] = []
            for i in range(warm):
                v = 0.0001 if i % 2 == 0 else -0.0001
                rows.append((T0 + (i + 1) * 60, v, v, v, v))
            spike_end = T0 + (warm + 1) * 60
            rows.append((spike_end, 0.005, 0.005, 0.005, 0.005))
            prem.write_month(venue=Venue.BINANCE, symbol="BTCUSDT", month="2026-01", rows=rows)
            _write_tape(
                ticks,
                start=spike_end - 60,
                seconds=2400,
                move_at=100,
                move=("100", "100.5", "100"),
                exit_close="99",
                exit_from=500,
            )
            bt = PremiumDislocationBacktester(ticks, prem)
            marks = np.asarray(bt.run(_proposal()))
            expected = (-1.0 * (99.0 / 100.0 - 1.0) - _COSTS) if fires else 0.0
            assert marks.sum() == pytest.approx(expected), f"warm={warm}"

    def test_nan_premium_is_no_signal(self, tmp_path: Path) -> None:
        ticks, prem = _stores(tmp_path)
        dec = _write_premium(prem, spike_nan=True)
        _write_tape(
            ticks,
            start=dec - 60,
            seconds=2400,
            move_at=100,
            move=("99.5", "100.5", "100"),
            exit_close="99",
            exit_from=500,
        )
        bt = PremiumDislocationBacktester(ticks, prem)
        assert np.asarray(bt.run(_proposal())).sum() == 0.0


class TestSpecAndBuilder:
    def test_threshold_positive(self) -> None:
        with pytest.raises(ValueError, match="threshold_sigma"):
            _PremiumDislocationSpec(PremiumDislocationConfig(threshold_sigma=Decimal("0")))

    def test_hold_bounded(self) -> None:
        with pytest.raises(ValueError, match="hold_minutes"):
            _PremiumDislocationSpec(PremiumDislocationConfig(hold_minutes=0))

    def test_builder_asserts_disjoint_roots(self, tmp_path: Path) -> None:
        ticks = TickStore(tmp_path / "ticks")
        prem = PremiumStore(tmp_path / "prem")
        with pytest.raises(ValueError):
            build_premium_backtesters(
                research_ticks=ticks,
                holdout_ticks=ticks,
                research_premium=prem,
                holdout_premium=PremiumStore(tmp_path / "prem2"),
            )
