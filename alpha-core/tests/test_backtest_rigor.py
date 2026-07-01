"""Backtest rigor tests (Phase 7 B7.3/B7.4)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alpha_core.backtest.rigor import audit_no_lookahead, stress_gate, walk_forward
from alpha_core.core.enums import AssetClass, OrderType, Side, Venue
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Signal, Tick
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.risk.limits import RiskConfig
from alpha_core.strategy.examples.placeholder import PlaceholderStrategy

SYMBOL = "NSE:RELIANCE"
START = datetime(2026, 6, 15, 3, 45, tzinfo=UTC)
ONE_MIN = timedelta(minutes=1)

COST_CONFIG: dict[str, object] = {
    "slippage": {
        "equity": {"type": "bps", "value": 5},
        "crypto_perp": {"type": "bps", "value": 8},
        "index_option": {"type": "ticks", "value": 2},
        "default_spread": {"equity": 0.0005, "crypto_perp": 0.0008, "index_option_ticks": 1},
        "stress_multiplier": 2,
    },
    "segments": {
        "equity_intraday": {"brokerage": {"pct": 0.0003, "flat": 20, "mode": "min"}},
        "index_option": {"brokerage": {"flat": 20, "mode": "flat"}},
        "crypto_perp": {"trading_fee": {"pct": 0.001, "side": "both"}},
    },
}


def _risk() -> RiskConfig:
    return RiskConfig.model_validate(
        {
            "base_capital": "100000",
            "limits": {
                "max_gross_exposure": "1.00",
                "max_position_per_instrument": "0.50",
                "max_concurrent_positions": 5,
                "max_order_value": "0.50",
                "max_orders_per_minute": 1000,
                "max_daily_loss_halt": "0.50",
                "max_loss_per_trade": "0.01",
                "per_segment_exposure_cap": "1.00",
            },
        }
    )


def _bars(closes: list[str]) -> list[Bar]:
    out: list[Bar] = []
    for i, c in enumerate(closes):
        close = Decimal(c)
        out.append(
            Bar(
                symbol=SYMBOL,
                venue=Venue.NSE,
                asset_class=AssetClass.EQUITY,
                start=START + i * ONE_MIN,
                interval=ONE_MIN,
                open=close - Decimal("1"),
                high=close + Decimal("2"),
                low=close - Decimal("2"),
                close=close,
                volume=Decimal("1000"),
            )
        )
    return out


def _instruments() -> dict[str, InstrumentMeta]:
    return {SYMBOL: InstrumentMeta(asset_class=AssetClass.EQUITY)}


class _LookAheadCheat(Strategy):
    """Cheats: buys on bar i iff the NEXT bar closes higher (uses the future)."""

    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars
        self._i = 0

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        i = self._i
        self._i += 1
        nxt = self._bars[i + 1] if i + 1 < len(self._bars) else None
        if nxt is not None and nxt.close > bar.close:
            return [
                Signal(
                    strategy_id="cheat",
                    symbol=bar.symbol,
                    asset_class=bar.asset_class,
                    side=Side.BUY,
                    quantity=Decimal("1"),
                    order_type=OrderType.MARKET,
                    created_at=bar.start + bar.interval,
                )
            ]
        return []

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []


# --- look-ahead audit ----------------------------------------------------------


def test_audit_passes_streaming_strategy() -> None:
    bars = _bars([str(100 + i) for i in range(10)])
    report = audit_no_lookahead(lambda _bars: PlaceholderStrategy(), bars)
    assert report.passed is True


def test_audit_catches_lookahead() -> None:
    bars = _bars([str(100 + i) for i in range(10)])
    report = audit_no_lookahead(lambda b: _LookAheadCheat(b), bars)
    assert report.passed is False
    assert "look-ahead" in report.detail


class _InteriorLookAheadCheat(Strategy):
    """Cheats only in the interior: buys on bar 2 iff bar 5 closes higher than
    bar 2. A last-bar-only audit never perturbs bar 5, so it misses this; the
    pivot sweep (F2) perturbs the tail from an interior pivot and catches it."""

    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars
        self._i = 0

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        i = self._i
        self._i += 1
        if i == 2 and len(self._bars) > 5 and self._bars[5].close > self._bars[2].close:
            return [
                Signal(
                    strategy_id="interior-cheat",
                    symbol=bar.symbol,
                    asset_class=bar.asset_class,
                    side=Side.BUY,
                    quantity=Decimal("1"),
                    order_type=OrderType.MARKET,
                    created_at=bar.start + bar.interval,
                )
            ]
        return []

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []


def test_audit_catches_interior_lookahead() -> None:
    bars = _bars([str(100 + i) for i in range(10)])  # rising: bar5 > bar2
    report = audit_no_lookahead(lambda b: _InteriorLookAheadCheat(b), bars)
    assert report.passed is False
    assert "look-ahead" in report.detail


# --- walk-forward --------------------------------------------------------------


async def test_walk_forward_windows() -> None:
    bars = _bars([str(100 + i) for i in range(30)])
    results = await walk_forward(
        bars=bars,
        make_strategy=lambda _b: PlaceholderStrategy(quantity=Decimal("10")),
        instruments=_instruments(),
        risk_config=_risk(),
        cost_config=COST_CONFIG,
        n_windows=3,
    )
    assert len(results) == 3
    assert all(r.stats.num_fills >= 0 for r in results)


# --- 2x stress gate ------------------------------------------------------------


async def test_stress_gate_survives_strong_edge() -> None:
    # steep rise -> profit big enough to survive 2x slippage
    bars = _bars([str(100 + 5 * i) for i in range(20)])
    res = await stress_gate(
        bars=bars,
        make_strategy=lambda _b: PlaceholderStrategy(quantity=Decimal("10")),
        instruments=_instruments(),
        risk_config=_risk(),
        cost_config=COST_CONFIG,
    )
    assert res.stressed.stats.final_pnl < res.base.stats.final_pnl  # stress costs more
    assert res.survived is True


async def test_stress_gate_rejects_no_edge() -> None:
    # falling series -> a loss -> does not survive
    bars = _bars([str(120 - i) for i in range(20)])
    res = await stress_gate(
        bars=bars,
        make_strategy=lambda _b: PlaceholderStrategy(quantity=Decimal("10")),
        instruments=_instruments(),
        risk_config=_risk(),
        cost_config=COST_CONFIG,
    )
    assert res.survived is False


# --- combined rigor report (P13.10) --------------------------------------------


async def test_rigor_report_bundles_verdict() -> None:
    from alpha_core.backtest.rigor import rigor_report

    bars = _bars([str(100 + 5 * i) for i in range(20)])  # strong rising edge
    report = await rigor_report(
        bars=bars,
        make_strategy=lambda _b: PlaceholderStrategy(quantity=Decimal("10")),
        instruments=_instruments(),
        risk_config=_risk(),
        cost_config=COST_CONFIG,
        n_windows=3,
    )
    assert report.structurally_sound is True  # placeholder never looks ahead
    assert len(report.walk_forward) == 3
    assert report.edge_robust == (
        report.no_lookahead and report.stress.survived and report.walk_forward_stable
    )


async def test_edge_robust_requires_walk_forward_stability() -> None:
    """A strategy can be look-ahead-clean and profitable overall (surviving 2x
    slippage) yet still fail the gate if any out-of-sample window loses money —
    the single-strategy gate must enforce walk-forward stability, matching the
    cross-instrument gate (F1)."""
    from alpha_core.backtest.rigor import rigor_report

    # 3 windows of 10 bars: rise, then a falling window, then a strong rise. The
    # whole series rises (overall profit survives 2x), but the middle window loses.
    closes = (
        [str(100 + i) for i in range(10)]  # window 0: 100..109 (up)
        + [str(110 - i) for i in range(10)]  # window 1: 110..101 (down -> losing)
        + [str(120 + 10 * i) for i in range(10)]  # window 2: 120..210 (strong up)
    )
    report = await rigor_report(
        bars=_bars(closes),
        make_strategy=lambda _b: PlaceholderStrategy(quantity=Decimal("10")),
        instruments=_instruments(),
        risk_config=_risk(),
        cost_config=COST_CONFIG,
        n_windows=3,
    )
    assert report.structurally_sound is True
    assert report.stress.survived is True  # profitable overall, survives 2x
    assert report.walk_forward_stable is False  # the middle window loses
    assert report.edge_robust is False  # ... so the gate must reject it


# --- input-guard edge cases ----------------------------------------------------


def test_audit_too_few_bars_passes_vacuously() -> None:
    # Fewer than 2 bars: there is no future tail to perturb, so the audit passes
    # by construction (a guard, not a verdict on the strategy).
    report = audit_no_lookahead(lambda _b: PlaceholderStrategy(), _bars(["100"]))
    assert report.passed is True
    assert "too few bars" in report.detail


async def test_walk_forward_rejects_non_positive_windows() -> None:
    with pytest.raises(ValueError, match="n_windows must be >= 1"):
        await walk_forward(
            bars=_bars([str(100 + i) for i in range(4)]),
            make_strategy=lambda _b: PlaceholderStrategy(quantity=Decimal("10")),
            instruments=_instruments(),
            risk_config=_risk(),
            cost_config=COST_CONFIG,
            n_windows=0,
        )


async def test_walk_forward_stops_when_a_window_is_empty() -> None:
    # 2 bars across 3 windows: window size floors to 1, so windows 0 and 1 each get
    # a bar and the 3rd is empty -> the loop breaks early and returns 2 results.
    results = await walk_forward(
        bars=_bars(["100", "101"]),
        make_strategy=lambda _b: PlaceholderStrategy(quantity=Decimal("10")),
        instruments=_instruments(),
        risk_config=_risk(),
        cost_config=COST_CONFIG,
        n_windows=3,
    )
    assert len(results) == 2
