"""F3 lead-lag fold — latency honesty, staleness embargo, direction, non-overlap — and the
tick seal's split + floor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.holdout import prior_window_starts
from alpha_core.data.tick_seal import seal_tick_store
from alpha_core.data.tick_store import TickStore, month_of
from alpha_core.helpers.config import DiscoveryCellConfig
from alpha_core.research.funding_window_backtester import taker_cost_per_side
from alpha_core.research.leadlag_backtester import (
    LEADLAG_CELLS,
    LEADLAG_TEMPLATES,
    LeadLagBacktester,
)
from alpha_core.research.promote import make_proposal
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.strategist import (
    CellSaturated,
    RandomProposer,
    Strategist,
    StrategyProposal,
)

_T0 = datetime(2025, 4, 1, 0, 0, tzinfo=UTC)
_ALT = "SOLUSDT"
_WINDOW = "solusdt-1s-ll"


def _bars(symbol: str, prices: dict[int, float], n: int, base: float = 100.0) -> list[Bar]:
    """n 1s bars from _T0; price changes at the offsets in ``prices`` persist afterwards."""
    out = []
    px = base
    for i in range(n):
        if i in prices:
            px = prices[i]
        c = Decimal(str(px))
        out.append(
            Bar(
                symbol=symbol,
                venue=Venue.BINANCE,
                asset_class=AssetClass.CRYPTO,
                start=_T0 + timedelta(seconds=i),
                interval=timedelta(seconds=1),
                open=c,
                high=c,
                low=c,
                close=c,
                volume=Decimal("1"),
            )
        )
    return out


def _store(tmp_path: Path, name: str, series: dict[str, list[Bar]]) -> TickStore:
    store = TickStore(tmp_path / name)
    for _, bars in series.items():
        by_month: dict[str, list[Bar]] = {}
        for b in bars:
            by_month.setdefault(month_of(b.start), []).append(b)
        for month, chunk in by_month.items():
            store.write_bars(chunk, month=month)
    return store


def _proposal(params: dict[str, object], window: str = _WINDOW) -> StrategyProposal:
    cell = DiscoveryCellConfig(
        market=AssetClass.CRYPTO,
        window=window,
        symbol=_ALT,
        venue=Venue.BINANCE,
        interval_seconds=1,
        starting_cash=Decimal("1000000"),
    )
    return make_proposal("leadlag_follow", params, cell, trial_index=1)  # type: ignore[arg-type]


_N = 9000  # 2.5h of seconds — enough for the 600-valid-sample sigma warmup


def _noise_btc(jump_at: int | None = None, jump_to: float = 101.0) -> list[Bar]:
    """BTC with tiny alternating noise (a stable, nonzero sigma) and an optional jump."""
    prices = {i: 100.0 + 0.001 * (1 if i % 2 else -1) for i in range(_N)}
    if jump_at is not None:
        for i in range(jump_at, _N):
            prices[i] = jump_to + 0.001 * (1 if i % 2 else -1)
    return _bars("BTCUSDT", prices, _N)


_PARAMS: dict[str, object] = {
    "lead_seconds": 15,
    "threshold_sigma": Decimal("4"),
    "hold_seconds": 60,
}


def test_follow_captures_slow_diffusion_long(tmp_path: Path) -> None:
    jump = 8000
    # alt follows BTC's +1% jump 30s later — slower than the 2s latency -> capturable
    alt = _bars(_ALT, {0: 100.0, jump + 30: 101.0}, _N)
    store = _store(tmp_path, "r", {"BTCUSDT": _noise_btc(jump_at=jump), _ALT: alt})
    marks = np.asarray(LeadLagBacktester(store).run(_proposal(_PARAMS)))
    total = marks.sum()
    # long entered ~jump+2s at 100.0, exited 60s later at 101.0: +1% minus two sides
    assert total == pytest.approx(0.01 - 2 * taker_cost_per_side(), abs=2e-4)


def test_follow_earns_nothing_when_diffusion_beats_latency(tmp_path: Path) -> None:
    jump = 8000
    # alt reprices at jump+1s — INSIDE the 2s latency; the fill happens post-move
    alt = _bars(_ALT, {0: 100.0, jump + 1: 101.0}, _N)
    store = _store(tmp_path, "r", {"BTCUSDT": _noise_btc(jump_at=jump), _ALT: alt})
    marks = np.asarray(LeadLagBacktester(store).run(_proposal(_PARAMS)))
    # entry at 101, exit at 101: zero edge, only costs
    assert marks.sum() == pytest.approx(-2 * taker_cost_per_side(), abs=2e-4)


def test_follow_shorts_a_leader_drop(tmp_path: Path) -> None:
    jump = 8000
    alt = _bars(_ALT, {0: 100.0, jump + 30: 99.0}, _N)
    store = _store(tmp_path, "r", {"BTCUSDT": _noise_btc(jump_at=jump, jump_to=99.0), _ALT: alt})
    marks = np.asarray(LeadLagBacktester(store).run(_proposal(_PARAMS)))
    assert marks.sum() == pytest.approx(0.01 - 2 * taker_cost_per_side(), abs=2e-4)


def test_stale_leader_tape_never_triggers(tmp_path: Path) -> None:
    jump = 8000
    # cut the BTC tape from jump-10 onward: the jump exists only in a print 20s late
    btc = [b for b in _noise_btc(jump_at=jump) if not (jump - 10 <= (b.start - _T0).seconds)]
    alt = _bars(_ALT, {0: 100.0, jump + 30: 101.0}, _N)
    store = _store(tmp_path, "r", {"BTCUSDT": btc, _ALT: alt})
    marks = np.asarray(LeadLagBacktester(store).run(_proposal(_PARAMS)))
    assert marks.sum() == 0.0  # signal stale at decision time -> embargoed


def test_non_overlap_one_trade_per_window(tmp_path: Path) -> None:
    jump = 8000
    alt = _bars(_ALT, {0: 100.0, jump + 30: 101.0}, _N)
    store = _store(tmp_path, "r", {"BTCUSDT": _noise_btc(jump_at=jump), _ALT: alt})
    params = dict(_PARAMS)
    params["hold_seconds"] = 300  # jump keeps triggering for 15s+, hold spans them all
    marks = np.asarray(LeadLagBacktester(store).run(_proposal(params)))
    # exactly ONE trade: one nonzero mark
    assert np.count_nonzero(marks) == 1


def test_unknown_cell_and_registry_shape(tmp_path: Path) -> None:
    store = _store(tmp_path, "r", {"BTCUSDT": _noise_btc(), _ALT: _bars(_ALT, {0: 100.0}, _N)})
    with pytest.raises(ValueError, match="unknown lead-lag cell"):
        LeadLagBacktester(store).run(_proposal(_PARAMS, window="nope-1s-ll"))
    assert set(LEADLAG_CELLS.values()) == {"SOLUSDT", "DOGEUSDT", "XRPUSDT", "ADAUSDT", "AVAXUSDT"}
    with ProposalLedger() as ledger:
        strategist = Strategist(
            ledger, proposer=RandomProposer(seed=3), templates=LEADLAG_TEMPLATES, max_attempts=500
        )
        count = 0
        while True:
            try:
                strategist.propose("leadlag_follow", market=AssetClass.CRYPTO, window=_WINDOW)
            except CellSaturated:
                break
            count += 1
        assert count == 8  # the pre-registered space: 2 x 2 x 2


# --- the tick seal ---------------------------------------------------------------------


def test_tick_seal_splits_at_boundary_and_floors(tmp_path: Path) -> None:
    # 10 days of hourly-spaced "1s-interval" bars across a month edge (small, fast)
    bars = []
    for i in range(240):
        c = Decimal("100")
        bars.append(
            Bar(
                symbol="BTCUSDT",
                venue=Venue.BINANCE,
                asset_class=AssetClass.CRYPTO,
                start=datetime(2025, 1, 28, tzinfo=UTC) + timedelta(hours=i),
                interval=timedelta(seconds=1),
                open=c,
                high=c,
                low=c,
                close=c,
                volume=Decimal("1"),
            )
        )
    raw = _store(tmp_path, "raw", {"BTCUSDT": bars})
    research = TickStore(tmp_path / "research")
    holdout = TickStore(tmp_path / "holdout")
    windows = seal_tick_store(raw, research, holdout, fraction=0.2)
    key = "BINANCE|BTCUSDT|1"
    w = windows[key]
    r = research.read_columns(venue=Venue.BINANCE, symbol="BTCUSDT", interval_seconds=1)
    h = holdout.read_columns(venue=Venue.BINANCE, symbol="BTCUSDT", interval_seconds=1)
    assert r.num_rows + h.num_rows == len(bars)
    r_starts = [s.astimezone(UTC) for s in r.column("start").to_pylist()]
    h_starts = [s.astimezone(UTC) for s in h.column("start").to_pylist()]
    assert max(r_starts) < w.start <= min(h_starts)  # row-exact split
    assert prior_window_starts(holdout.root)[key] == w.start  # manifest written

    # extend history BACKWARD; the boundary must not move earlier (the monotonic floor)
    older = []
    for i in range(240):
        c = Decimal("100")
        older.append(
            Bar(
                symbol="BTCUSDT",
                venue=Venue.BINANCE,
                asset_class=AssetClass.CRYPTO,
                start=datetime(2024, 11, 1, tzinfo=UTC) + timedelta(hours=i),
                interval=timedelta(seconds=1),
                open=c,
                high=c,
                low=c,
                close=c,
                volume=Decimal("1"),
            )
        )
    by_month: dict[str, list[Bar]] = {}
    for b in older:
        by_month.setdefault(month_of(b.start), []).append(b)
    for month, chunk in by_month.items():
        raw.write_bars(chunk, month=month)
    windows2 = seal_tick_store(raw, research, holdout, fraction=0.2)
    assert windows2[key].start >= w.start  # floored: never backward
