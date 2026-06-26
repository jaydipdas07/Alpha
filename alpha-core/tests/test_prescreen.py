"""vectorbt coarse pre-screen (R7, B1a.8) — cull losers cheaply before CPCV."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from alpha_core.core.enums import AssetClass, OrderType, Side, Venue
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Signal, Tick
from alpha_core.helpers.config import PrescreenConfig, load_rigor_config
from alpha_core.research.prescreen import PrescreenResult, prescreen

SYM = "BTCUSDT"
START = datetime(2026, 6, 26, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _bars(closes: Sequence[int]) -> list[Bar]:
    out: list[Bar] = []
    for i, raw in enumerate(closes):
        c = Decimal(raw)
        out.append(
            Bar(
                symbol=SYM,
                venue=Venue.BINANCE,
                asset_class=AssetClass.CRYPTO,
                start=START + i * HOUR,
                interval=HOUR,
                open=c,
                high=c + Decimal("1"),
                low=c - Decimal("1"),
                close=c,
                volume=Decimal("10"),
            )
        )
    return out


class _BuyHold(Strategy):
    """Buys the first bar it sees, then holds."""

    def __init__(self) -> None:
        self._opened = False

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        if self._opened:
            return []
        self._opened = True
        return [_sig(bar, Side.BUY)]

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []


class _Churn(Strategy):
    """Buys on even bars, sells on odd — exercises both entries and exits."""

    def __init__(self) -> None:
        self._i = 0

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        side = Side.BUY if self._i % 2 == 0 else Side.SELL
        self._i += 1
        return [_sig(bar, side)]

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []


def _sig(bar: Bar, side: Side) -> Signal:
    return Signal(
        strategy_id="s",
        symbol=bar.symbol,
        asset_class=bar.asset_class,
        side=side,
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
        created_at=bar.start + bar.interval,
    )


def test_prescreen_passes_a_winner() -> None:
    result = prescreen(lambda _b: _BuyHold(), _bars(range(100, 130)), min_sharpe=0.0, n_windows=3)
    assert isinstance(result, PrescreenResult)
    assert result.passed is True  # a rising buy-and-hold has a positive coarse Sharpe
    assert result.median_sharpe > 0
    assert len(result.window_sharpes) == 3


def test_prescreen_culls_a_loser() -> None:
    result = prescreen(
        lambda _b: _BuyHold(), _bars(range(130, 100, -1)), min_sharpe=0.0, n_windows=3
    )
    assert result.passed is False  # a falling buy-and-hold loses -> culled before CPCV
    assert result.median_sharpe < 0


def test_prescreen_honours_the_config_floor() -> None:
    cfg = load_rigor_config()
    result = prescreen(
        lambda _b: _BuyHold(),
        _bars(range(100, 130)),
        min_sharpe=cfg.prescreen.min_sharpe,
        n_windows=cfg.prescreen.n_windows,
    )
    assert result.passed is True


def test_prescreen_handles_exits() -> None:
    result = prescreen(lambda _b: _Churn(), _bars(range(100, 130)), min_sharpe=0.0, n_windows=3)
    assert isinstance(result.passed, bool)  # buy/sell churn runs through the vectorbt sim
    assert len(result.window_sharpes) == 3


def test_prescreen_flat_market_is_zero_sharpe() -> None:
    result = prescreen(lambda _b: _BuyHold(), _bars([100] * 20), min_sharpe=0.0, n_windows=2)
    assert result.median_sharpe == 0.0  # no dispersion -> Sharpe 0


def test_prescreen_culls_when_too_little_data() -> None:
    result = prescreen(
        lambda _b: _BuyHold(), _bars([100, 101, 102, 103]), min_sharpe=0.0, n_windows=3
    )
    assert result.passed is False  # no window had >= 2 bars
    assert result.window_sharpes == ()


def test_prescreen_rejects_bad_windows() -> None:
    with pytest.raises(ValueError, match="n_windows"):
        prescreen(lambda _b: _BuyHold(), _bars([100, 101]), min_sharpe=0.0, n_windows=0)


def test_prescreen_config() -> None:
    cfg = load_rigor_config()
    assert cfg.prescreen.n_windows >= 1
    with pytest.raises(ValidationError):
        PrescreenConfig(min_sharpe=0.0, n_windows=0)
