"""R6 — cross-instrument rigor: cross-sectional look-ahead, walk-forward, stress."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alpha_core.backtest.rigor import (
    audit_no_lookahead_portfolio,
    portfolio_rigor_report,
    portfolio_stress_gate,
    portfolio_walk_forward,
)
from alpha_core.core.enums import AssetClass, OrderType, Side, Venue
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Signal, Tick
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.helpers.config import PortfolioConfig
from alpha_core.portfolio.construction import WeightAllocator
from alpha_core.risk.limits import RiskConfig

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


def _portfolio_cfg() -> PortfolioConfig:
    return PortfolioConfig.model_validate(
        {
            "allocation": {
                "method": "equal_weight",
                "top_k": 2,
                "max_weight_per_name": "0.5",
                "gross_cap": "1.0",
            },
            "rebalance": {
                "no_trade_band": "0.02",
                "turnover_cap": "10.0",
                "min_position_weight": "0.05",
            },
        }
    )


def _risk() -> RiskConfig:
    return RiskConfig.model_validate(
        {
            "base_capital": "1000000",
            "limits": {
                "max_gross_exposure": "1.00",
                "max_position_per_instrument": "0.60",
                "max_concurrent_positions": 10,
                "max_order_value": "0.60",
                "max_orders_per_minute": 100000,
                "max_daily_loss_halt": "0.90",
                "max_loss_per_trade": "1.00",
                "per_segment_exposure_cap": "1.00",
            },
        }
    )


def _instruments() -> dict[str, InstrumentMeta]:
    return {
        s: InstrumentMeta(asset_class=AssetClass.EQUITY, tick_size=Decimal("0.05"))
        for s in ("NSE:A", "NSE:B")
    }


def _bars() -> list[Bar]:
    out: list[Bar] = []
    for i in range(8):
        for sym, base in (("NSE:A", 100), ("NSE:B", 200)):
            c = Decimal(base + i)
            out.append(
                Bar(
                    symbol=sym,
                    venue=Venue.NSE,
                    asset_class=AssetClass.EQUITY,
                    start=START + i * ONE_MIN,
                    interval=ONE_MIN,
                    open=c,
                    high=c + Decimal("2"),
                    low=c - Decimal("2"),
                    close=c,
                    volume=Decimal("1000"),
                )
            )
    return out


class _OneShotBuy(Strategy):
    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        n = self._seen.get(bar.symbol, 0) + 1
        self._seen[bar.symbol] = n
        if n == 2:
            return [
                Signal(
                    strategy_id="s1",
                    symbol=bar.symbol,
                    asset_class=bar.asset_class,
                    side=Side.BUY,
                    quantity=Decimal("1"),
                    order_type=OrderType.MARKET,
                    created_at=bar.start + bar.interval,
                    score=Decimal("1"),
                )
            ]
        return []

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []


class _LookAheadCheat(Strategy):
    """Buys on bar i iff the NEXT bar closes higher (peeks at the future)."""

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
                    score=Decimal("1"),
                )
            ]
        return []

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []


# --- cross-sectional look-ahead audit ------------------------------------------


def test_portfolio_audit_passes_clean_strategies() -> None:
    report = audit_no_lookahead_portfolio(lambda _b: [_OneShotBuy()], _bars())
    assert report.passed


def test_portfolio_audit_catches_cheat() -> None:
    report = audit_no_lookahead_portfolio(lambda b: [_LookAheadCheat(b)], _bars())
    assert not report.passed
    assert "look-ahead" in report.detail


# --- stress / walk-forward / report --------------------------------------------


@pytest.mark.asyncio
async def test_portfolio_stress_gate_runs() -> None:
    result = await portfolio_stress_gate(
        bars=_bars(),
        make_strategies=lambda _b: [_OneShotBuy()],
        make_allocator=lambda: WeightAllocator(_portfolio_cfg()),
        portfolio_config=_portfolio_cfg(),
        instruments=_instruments(),
        risk_config=_risk(),
        cost_config=COST_CONFIG,
    )
    # 2x slippage worsens fills, so the stressed P&L cannot beat the base.
    assert result.stressed.stats.final_pnl <= result.base.stats.final_pnl
    assert result.base.stats.traded_notional > 0
    assert result.base.stats.turnover_ratio > 0


@pytest.mark.asyncio
async def test_portfolio_walk_forward_windows() -> None:
    windows = await portfolio_walk_forward(
        bars=_bars(),
        make_strategies=lambda _b: [_OneShotBuy()],
        make_allocator=lambda: WeightAllocator(_portfolio_cfg()),
        portfolio_config=_portfolio_cfg(),
        instruments=_instruments(),
        risk_config=_risk(),
        cost_config=COST_CONFIG,
        n_windows=2,
    )
    assert len(windows) == 2


@pytest.mark.asyncio
async def test_portfolio_walk_forward_empty_bars() -> None:
    windows = await portfolio_walk_forward(
        bars=[],
        make_strategies=lambda _b: [_OneShotBuy()],
        make_allocator=lambda: WeightAllocator(_portfolio_cfg()),
        portfolio_config=_portfolio_cfg(),
        instruments=_instruments(),
        risk_config=_risk(),
        cost_config=COST_CONFIG,
    )
    assert windows == []


@pytest.mark.asyncio
async def test_portfolio_walk_forward_more_windows_than_bars() -> None:
    # n_windows far exceeds the distinct timestamps → some windows empty (break).
    windows = await portfolio_walk_forward(
        bars=_bars(),
        make_strategies=lambda _b: [_OneShotBuy()],
        make_allocator=lambda: WeightAllocator(_portfolio_cfg()),
        portfolio_config=_portfolio_cfg(),
        instruments=_instruments(),
        risk_config=_risk(),
        cost_config=COST_CONFIG,
        n_windows=20,
    )
    assert 0 < len(windows) <= 8


@pytest.mark.asyncio
async def test_portfolio_walk_forward_rejects_zero_windows() -> None:
    with pytest.raises(ValueError, match="n_windows"):
        await portfolio_walk_forward(
            bars=_bars(),
            make_strategies=lambda _b: [_OneShotBuy()],
            make_allocator=lambda: WeightAllocator(_portfolio_cfg()),
            portfolio_config=_portfolio_cfg(),
            instruments=_instruments(),
            risk_config=_risk(),
            cost_config=COST_CONFIG,
            n_windows=0,
        )


@pytest.mark.asyncio
async def test_portfolio_rigor_report_bundles() -> None:
    report = await portfolio_rigor_report(
        bars=_bars(),
        make_strategies=lambda _b: [_OneShotBuy()],
        make_allocator=lambda: WeightAllocator(_portfolio_cfg()),
        portfolio_config=_portfolio_cfg(),
        instruments=_instruments(),
        risk_config=_risk(),
        cost_config=COST_CONFIG,
        n_windows=2,
    )
    assert report.structurally_sound  # clean strategies → no look-ahead
    assert len(report.walk_forward) == 2
    assert isinstance(report.edge_robust, bool)
    assert isinstance(report.walk_forward_stable, bool)
    assert report.turnover_ratio >= 0


@pytest.mark.asyncio
async def test_portfolio_rigor_flags_lookahead() -> None:
    report = await portfolio_rigor_report(
        bars=_bars(),
        make_strategies=lambda b: [_LookAheadCheat(b)],
        make_allocator=lambda: WeightAllocator(_portfolio_cfg()),
        portfolio_config=_portfolio_cfg(),
        instruments=_instruments(),
        risk_config=_risk(),
        cost_config=COST_CONFIG,
    )
    assert not report.structurally_sound  # the cheat is caught
    assert not report.edge_robust
