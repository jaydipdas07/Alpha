"""G5 OI-flush fold tests — publish embargo, direction, non-overlap, staleness, costs."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.holdout import HoldoutStore
from alpha_core.data.metrics_store import MetricsRow, MetricsStore
from alpha_core.data.store import BarStore
from alpha_core.research.cost_scenarios import cost_per_side
from alpha_core.research.oi_backtester import (
    OI_CELLS,
    OI_TEMPLATES,
    OiFlushBacktester,
    build_oi_backtesters,
)
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.strategist import (
    CellSaturated,
    RandomProposer,
    Strategist,
    StrategyProposal,
)

T0 = int(datetime(2026, 1, 5, tzinfo=UTC).timestamp())
_H = 3600
N_BARS = 300
K = 250  # the flush bar (well past the 7-day sigma warmup)


def _proposal(window_h: int = 4, sigma: str = "3", hold_h: int = 4) -> StrategyProposal:
    return StrategyProposal(
        template="oi_flush_reversion",
        params={
            "window_hours": Decimal(window_h),
            "threshold_sigma": Decimal(sigma),
            "hold_hours": Decimal(hold_h),
        },
        market=AssetClass.CRYPTO,
        window="btcusdt-1h-oi",
        trial_index=1,
        fingerprint="t",
    )


def _bars(closes: dict[int, float]) -> list[Bar]:
    """N_BARS flat hourly bars at 100.0, overridden per index by ``closes``."""
    out: list[Bar] = []
    for i in range(N_BARS):
        p = Decimal(str(closes.get(i, 100.0)))
        out.append(
            Bar(
                symbol="BTCUSDT",
                venue=Venue.BINANCE,
                asset_class=AssetClass.CRYPTO,
                start=datetime.fromtimestamp(T0 + i * _H, tz=UTC),
                interval=timedelta(hours=1),
                open=p,
                high=p,
                low=p,
                close=p,
                volume=Decimal("1"),
            )
        )
    return out


def _metrics(tmp_path: Path, *, drop_from: int | None, end: int | None = None) -> MetricsStore:
    """5-min OI tape from T0-5h: base 1000, base 800 from ``drop_from`` on, phase-irregular
    ±0.1% noise (nonzero sigma; |z| stays < 3 at baseline). Ratio columns are ALL NaN —
    the fold must run on the OI-only 2022-era shape."""
    store = MetricsStore(tmp_path / "metrics")
    start = T0 - 5 * _H
    stop = end if end is not None else T0 + (N_BARS + 1) * _H
    rows: list[MetricsRow] = []
    for j, epoch in enumerate(range(start, stop + 1, 300)):
        base = 800.0 if (drop_from is not None and epoch >= drop_from) else 1000.0
        oi = base * (1.0 + 0.001 * math.sin(1.7 * j))
        rows.append((epoch, oi, oi * 90_000.0, math.nan, math.nan, math.nan, math.nan))
    by_month: dict[str, list[MetricsRow]] = {}
    for row in rows:
        m = datetime.fromtimestamp(row[0], tz=UTC)
        by_month.setdefault(f"{m.year:04d}-{m.month:02d}", []).append(row)
    for month, mrows in by_month.items():
        store.write_month(venue=Venue.BINANCE, symbol="BTCUSDT", month=month, rows=mrows)
    return store


def _dec(i: int) -> int:
    """Decision instant of bar i (its close)."""
    return T0 + (i + 1) * _H


@pytest.mark.parametrize("scenario", ["taker", "maker"])
def test_long_liquidation_flush_buys_the_dip_exact_pnl(tmp_path: Path, scenario: str) -> None:
    """OI -20% while price falls ⇒ BUY at the decision close, exit 4h later; one trade,
    exact net pnl, non-overlap consumes the echo triggers while the position is open."""
    closes = {K - 4: 99.0, K - 3: 98.0, K - 2: 97.0, K - 1: 96.0, K: 95.0, K + 4: 97.0}
    bars = _bars(closes)
    metrics = _metrics(tmp_path, drop_from=_dec(K) - 900)
    bt = OiFlushBacktester(lambda s: bars, metrics, cost_scenario=scenario)
    marks = np.asarray(bt.run(_proposal()))
    expected = 97.0 / 95.0 - 1.0 - 2.0 * cost_per_side(scenario)
    assert np.count_nonzero(marks) == 1
    assert marks[K + 4] == pytest.approx(expected)


def test_short_squeeze_flush_sells_the_rip_exact_pnl(tmp_path: Path) -> None:
    """OI -20% while price RISES ⇒ the same flush signal takes the SHORT side."""
    closes = {K - 4: 101.0, K - 3: 102.0, K - 2: 103.0, K - 1: 104.0, K: 105.0, K + 4: 103.0}
    bars = _bars(closes)
    metrics = _metrics(tmp_path, drop_from=_dec(K) - 900)
    bt = OiFlushBacktester(lambda s: bars, metrics)
    marks = np.asarray(bt.run(_proposal()))
    expected = -(103.0 / 105.0 - 1.0) - 2.0 * cost_per_side("taker")
    assert np.count_nonzero(marks) == 1
    assert marks[K + 4] == pytest.approx(expected)


def test_publish_embargo_defers_an_unpublished_snapshot_one_bar(tmp_path: Path) -> None:
    """A crash snapshot stamped INSIDE (t-300, t] is invisible at bar K's close (the live
    API may not have served it yet) — the trade fires at K+1, at K+1's prices."""
    closes = {
        K - 4: 99.0,
        K - 3: 98.0,
        K - 2: 97.0,
        K - 1: 96.0,
        K: 95.0,
        K + 1: 94.0,
        K + 5: 98.0,
    }
    bars = _bars(closes)
    metrics = _metrics(tmp_path, drop_from=_dec(K) - 200)  # published after t-300
    bt = OiFlushBacktester(lambda s: bars, metrics)
    marks = np.asarray(bt.run(_proposal()))
    expected = 98.0 / 94.0 - 1.0 - 2.0 * cost_per_side("taker")  # K+1 entry, NOT K's
    assert np.count_nonzero(marks) == 1
    assert marks[K + 5] == pytest.approx(expected)


def test_stale_metrics_era_yields_no_signal(tmp_path: Path) -> None:
    """A dead metrics tape (>30 min old at decision) must produce silence, not a fill
    off a stale snapshot — the archive has real holes."""
    closes = {K - 4: 99.0, K - 3: 98.0, K - 2: 97.0, K - 1: 96.0, K: 95.0, K + 4: 97.0}
    bars = _bars(closes)
    metrics = _metrics(tmp_path, drop_from=_dec(K) - 900, end=_dec(K) - 7200)
    bt = OiFlushBacktester(lambda s: bars, metrics)
    marks = np.asarray(bt.run(_proposal()))
    assert not np.any(marks)


def test_no_flush_no_trades(tmp_path: Path) -> None:
    """Baseline OI noise + a price decline alone stays under 3 sigma: zero trades."""
    closes = {K - 4: 99.0, K - 3: 98.0, K - 2: 97.0, K - 1: 96.0, K: 95.0, K + 4: 97.0}
    bars = _bars(closes)
    metrics = _metrics(tmp_path, drop_from=None)
    bt = OiFlushBacktester(lambda s: bars, metrics)
    marks = np.asarray(bt.run(_proposal()))
    assert not np.any(marks)


def test_registration_space_is_exactly_eight_configs() -> None:
    """The pre-registered grid: {4,8}h x {2,3}sigma x {4,12}h — saturation proves it."""
    seen: set[tuple[str, ...]] = set()
    with ProposalLedger() as ledger:
        strategist = Strategist(
            ledger,
            proposer=RandomProposer(seed=13),
            templates=OI_TEMPLATES,
            max_attempts=500,
        )
        while True:
            try:
                p = strategist.propose(
                    "oi_flush_reversion", market=AssetClass.CRYPTO, window="btcusdt-1h-oi"
                )
            except CellSaturated:
                break
            assert p.params["window_hours"] in {Decimal("4"), Decimal("8")}
            assert p.params["threshold_sigma"] in {Decimal("2"), Decimal("3")}
            assert p.params["hold_hours"] in {Decimal("4"), Decimal("12")}
            seen.add(tuple(str(p.params[k]) for k in sorted(p.params)))
    assert len(seen) == 8


def test_unknown_cell_rejected(tmp_path: Path) -> None:
    metrics = MetricsStore(tmp_path / "m")
    bt = OiFlushBacktester(lambda s: [], metrics)
    bad = StrategyProposal(
        template="oi_flush_reversion",
        params=_proposal().params,
        market=AssetClass.CRYPTO,
        window="dogeusdt-1h-oi",
        trial_index=1,
        fingerprint="t",
    )
    with pytest.raises(ValueError, match="unknown oi cell"):
        bt.run(bad)
    assert "dogeusdt-1h-oi" not in OI_CELLS


def test_build_pair_rejects_shared_roots(tmp_path: Path) -> None:
    """TEST-3: one mispointed env var must never make the "holdout" read in-sample —
    on the metrics pair as much as the bar pair."""
    with pytest.raises(ValueError, match="disjoint"):
        build_oi_backtesters(
            research_store=BarStore(tmp_path / "bars"),
            holdout_store=HoldoutStore(tmp_path / "hold"),
            research_metrics=MetricsStore(tmp_path / "m"),
            holdout_metrics=MetricsStore(tmp_path / "m"),
        )
    with pytest.raises(ValueError, match="disjoint"):
        build_oi_backtesters(
            research_store=BarStore(tmp_path / "same"),
            holdout_store=HoldoutStore(tmp_path / "same"),
            research_metrics=MetricsStore(tmp_path / "m1"),
            holdout_metrics=MetricsStore(tmp_path / "m2"),
        )
