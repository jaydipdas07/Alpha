"""F4 cascade-reversion fold — direction, thresholding, latency, and the event loader."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.tick_store import TickStore, month_of
from alpha_core.helpers.config import DiscoveryCellConfig
from alpha_core.research.funding_window_backtester import taker_cost_per_side
from alpha_core.research.liquidation_backtester import (
    LIQ_CELLS,
    LIQ_TEMPLATES,
    LiquidationBacktester,
    load_liq_events,
)
from alpha_core.research.promote import make_proposal
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.strategist import (
    CellSaturated,
    RandomProposer,
    Strategist,
    StrategyProposal,
)

_T0 = datetime(2024, 3, 1, 0, 0, tzinfo=UTC)
_N = 20_000  # ~5.5h of 1s bars (clears the 4h z warmup)
_CASCADE = 16_000  # cascade onset (bar offset)


def _write_events(path: Path, contract: str, events: list[tuple[int, str, float, float]]) -> None:
    """events: (offset_seconds, side, quantity, price)."""
    table = pa.Table.from_pylist(
        [
            {
                "contract": contract,
                "ts": _T0 + timedelta(seconds=off),
                "side": side,
                "quantity": qty,
                "price": px,
            }
            for off, side, qty, px in events
        ],
        schema=pa.schema(
            [
                ("contract", pa.string()),
                ("ts", pa.timestamp("us", tz="UTC")),
                ("side", pa.string()),
                ("quantity", pa.float64()),
                ("price", pa.float64()),
            ]
        ),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


def _baseline_events(side_cycle: bool = True) -> list[tuple[int, str, float, float]]:
    """A small forced order every 10 minutes — a stable, nonzero z denominator."""
    out = []
    for i, off in enumerate(range(0, _N, 600)):
        side = "BUY" if (side_cycle and i % 2 == 0) else "SELL"
        out.append((off, side, 1.0, 100.0))
    return out


def _bars(prices: dict[int, float]) -> list[Bar]:
    out = []
    px = 100.0
    for i in range(_N):
        if i in prices:
            px = prices[i]
        c = Decimal(str(px))
        out.append(
            Bar(
                symbol="BTCUSDT",
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


def _ticks(tmp_path: Path, bars: list[Bar]) -> TickStore:
    store = TickStore(tmp_path / "ticks")
    by_month: dict[str, list[Bar]] = {}
    for b in bars:
        by_month.setdefault(month_of(b.start), []).append(b)
    for month, chunk in by_month.items():
        store.write_bars(chunk, month=month)
    return store


def _proposal(params: dict[str, object], window: str = "btcusdt-1s-lr") -> StrategyProposal:
    cell = DiscoveryCellConfig(
        market=AssetClass.CRYPTO,
        window=window,
        symbol="BTCUSDT",
        venue=Venue.BINANCE,
        interval_seconds=1,
        starting_cash=Decimal("1000000"),
    )
    return make_proposal("liq_cascade_revert", params, cell, trial_index=1)  # type: ignore[arg-type]


_PARAMS: dict[str, object] = {
    "window_seconds": 60,
    "z_min": Decimal("4"),
    "hold_seconds": 300,
}


def test_reverts_long_after_a_sell_cascade(tmp_path: Path) -> None:
    # longs force-closed (SELL liq cluster) push price 100 -> 98; it reverts to 99.5
    events = _baseline_events() + [(_CASCADE + i, "SELL", 100.0, 99.0) for i in range(0, 30, 3)]
    _write_events(tmp_path / "liq" / "BTCUSD_PERP.parquet", "BTCUSD_PERP", events)
    bars = _bars({0: 100.0, _CASCADE: 98.0, _CASCADE + 150: 99.5})
    bt = LiquidationBacktester(_ticks(tmp_path, bars), tmp_path / "liq")
    marks = np.asarray(bt.run(_proposal(_PARAMS)))
    # long entered ~98 shortly after the cluster, exited ~99.5 after the 300s hold
    assert marks.sum() > 0.012 - 2 * taker_cost_per_side()
    assert marks.sum() < 0.016


def test_shorts_a_buy_cascade(tmp_path: Path) -> None:
    # shorts squeezed (BUY liq cluster) spike price 100 -> 102; it settles back to 100.5
    events = _baseline_events() + [(_CASCADE + i, "BUY", 100.0, 101.0) for i in range(0, 30, 3)]
    _write_events(tmp_path / "liq" / "BTCUSD_PERP.parquet", "BTCUSD_PERP", events)
    bars = _bars({0: 100.0, _CASCADE: 102.0, _CASCADE + 150: 100.5})
    bt = LiquidationBacktester(_ticks(tmp_path, bars), tmp_path / "liq")
    marks = np.asarray(bt.run(_proposal(_PARAMS)))
    assert marks.sum() > 0.012 - 2 * taker_cost_per_side()


def test_baseline_flow_alone_never_triggers(tmp_path: Path) -> None:
    _write_events(tmp_path / "liq" / "BTCUSD_PERP.parquet", "BTCUSD_PERP", _baseline_events())
    bars = _bars({0: 100.0})
    bt = LiquidationBacktester(_ticks(tmp_path, bars), tmp_path / "liq")
    marks = np.asarray(bt.run(_proposal(_PARAMS)))
    assert marks.sum() == 0.0


def test_loader_signs_and_orders_events(tmp_path: Path) -> None:
    _write_events(
        tmp_path / "liq" / "BTCUSD_PERP.parquet",
        "BTCUSD_PERP",
        [(100, "SELL", 2.0, 50.0), (50, "BUY", 3.0, 40.0)],
    )
    ts, flow = load_liq_events(tmp_path / "liq", "BTCUSD_PERP")
    assert list(ts) == [
        int((_T0 + timedelta(seconds=50)).timestamp()),
        int((_T0 + timedelta(seconds=100)).timestamp()),
    ]
    assert list(flow) == [120.0, -100.0]  # BUY positive (upward push), SELL negative


def test_unknown_cell_and_registry_shape(tmp_path: Path) -> None:
    _write_events(tmp_path / "liq" / "BTCUSD_PERP.parquet", "BTCUSD_PERP", _baseline_events())
    bt = LiquidationBacktester(_ticks(tmp_path, _bars({0: 100.0})), tmp_path / "liq")
    with pytest.raises(ValueError, match="unknown liquidation cell"):
        bt.run(_proposal(_PARAMS, window="nope-1s-lr"))
    assert set(LIQ_CELLS) == {"btcusdt-1s-lr", "ethusdt-1s-lr"}
    with ProposalLedger() as ledger:
        strategist = Strategist(
            ledger, proposer=RandomProposer(seed=5), templates=LIQ_TEMPLATES, max_attempts=500
        )
        count = 0
        while True:
            try:
                strategist.propose(
                    "liq_cascade_revert", market=AssetClass.CRYPTO, window="btcusdt-1s-lr"
                )
            except CellSaturated:
                break
            count += 1
        assert count == 8
