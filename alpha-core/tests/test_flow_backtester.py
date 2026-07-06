"""G2 flow-continuation fold tests — latency honesty, imbalance math, non-overlap, costs."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.flow_store import FlowStore
from alpha_core.data.tick_store import TickStore
from alpha_core.research.cost_scenarios import cost_per_side
from alpha_core.research.flow_backtester import FLOW_TEMPLATES, FlowBacktester
from alpha_core.research.strategist import StrategyProposal

T0 = int(datetime(2026, 1, 5, tzinfo=UTC).timestamp())


def _proposal(lookback: int = 60, sigma: str = "2", hold: int = 60) -> StrategyProposal:
    return StrategyProposal(
        template="flow_continuation",
        params={
            "lookback_seconds": Decimal(lookback),
            "threshold_sigma": Decimal(sigma),
            "hold_seconds": Decimal(hold),
        },
        market=AssetClass.CRYPTO,
        window="btcusdt-1s-flow",
        trial_index=1,
        fingerprint="t",
    )


def _bar(epoch: int, price: str) -> Bar:
    p = Decimal(price)
    return Bar(
        symbol="BTCUSDT",
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        start=datetime.fromtimestamp(epoch, tz=UTC),
        interval=__import__("datetime").timedelta(seconds=1),
        open=p,
        high=p,
        low=p,
        close=p,
        volume=Decimal("1"),
    )


def _stores(tmp_path: Path) -> tuple[TickStore, FlowStore]:
    return TickStore(tmp_path / "ticks"), FlowStore(tmp_path / "flows")


def _write_quiet_tape(
    ticks: TickStore, flows: FlowStore, *, seconds: int, burst_at: int | None = None
) -> None:
    """A flat $100 tape; optionally one massive buy burst with a LATER price step.

    With a burst: the background flow alternates mildly (sigma > 0 so the threshold is
    live), the burst fires at ``burst_at``, and the price steps to 101 only at
    ``burst_at + 10`` — safely AFTER the 2s-latency entry (still at 100) and before the
    60s-hold exit (at 101): the long's gross is exactly +1%. Without a burst: perfectly
    balanced flow (sigma == 0) — the threshold can never arm.
    """
    step_at = None if burst_at is None else burst_at + 10
    bars = [
        _bar(T0 + i, "100" if step_at is None or i < step_at else "101")
        for i in range(seconds)
    ]
    ticks.write_bars(bars, month="2026-01")
    rows = []
    for i in range(seconds):
        if burst_at is not None and burst_at <= i < burst_at + 5:
            rows.append((T0 + i, 500.0, 1.0))  # one-sided taker-buy burst
        elif burst_at is not None:
            noisy = (1.2, 0.8) if i % 2 == 0 else (0.8, 1.2)  # mild alternating imbalance
            rows.append((T0 + i, noisy[0], noisy[1]))
        else:
            rows.append((T0 + i, 1.0, 1.0))  # balanced background
    flows.write_month(
        venue=Venue.BINANCE,
        symbol="BTCUSDT",
        interval_seconds=1,
        month="2026-01",
        rows=rows,
    )


def test_balanced_flow_never_triggers(tmp_path: Path) -> None:
    ticks, flows = _stores(tmp_path)
    _write_quiet_tape(ticks, flows, seconds=1200)
    fold = FlowBacktester(ticks, flows)
    marks = np.asarray(fold.run(_proposal()))
    assert not marks.any()  # dead-balanced flow: zero trades, zero marks


def test_burst_enters_with_latency_and_charges_costs(tmp_path: Path) -> None:
    ticks, flows = _stores(tmp_path)
    _write_quiet_tape(ticks, flows, seconds=1200, burst_at=600)
    fold = FlowBacktester(ticks, flows)
    marks = np.asarray(fold.run(_proposal(lookback=60, sigma="2", hold=60)))
    nz = marks[marks != 0.0]
    assert len(nz) >= 1
    # the long entered at 100 (>= 2s after the trigger) and exited at 101: +1% gross,
    # minus the taker round trip from the ONE config home
    expected = 0.01 - 2.0 * cost_per_side("taker")
    assert nz[0] == pytest.approx(expected, abs=1e-9)


def test_maker_scenario_reprices_the_same_trade(tmp_path: Path) -> None:
    ticks, flows = _stores(tmp_path)
    _write_quiet_tape(ticks, flows, seconds=1200, burst_at=600)
    taker = np.asarray(FlowBacktester(ticks, flows).run(_proposal()))
    maker = np.asarray(
        FlowBacktester(ticks, flows, cost_scenario="maker").run(_proposal())
    )
    t_pnl = taker[taker != 0.0][0]
    m_pnl = maker[maker != 0.0][0]
    assert m_pnl - t_pnl == pytest.approx(
        2.0 * (cost_per_side("taker") - cost_per_side("maker")), abs=1e-12
    )


def test_registered_space_is_eight_configs() -> None:
    space = FLOW_TEMPLATES["flow_continuation"].param_space
    counts = 1
    for rng in space.values():
        lo, hi, step = rng.low, rng.high, rng.step  # type: ignore[union-attr]
        counts *= int((hi - lo) / step) + 1
    assert counts == 8


def test_missing_flow_data_fails_loud(tmp_path: Path) -> None:
    ticks, flows = _stores(tmp_path)
    ticks.write_bars([_bar(T0 + i, "100") for i in range(10)], month="2026-01")
    with pytest.raises(ValueError, match="no flow data"):
        FlowBacktester(ticks, flows).run(_proposal())
