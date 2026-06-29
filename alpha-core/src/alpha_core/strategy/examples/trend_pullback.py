"""Trend + RSI-pullback confluence (richer templates, M3.0).

A two-signal strategy: a slow SMA sets the trend (close above = up, below = down); enter only when
trend and a momentum *pullback* agree — go long when the trend is up **and** RSI has dipped below
``pullback_level`` (buy the dip in an uptrend), short when the trend is down and RSI has rallied
above its mirror. Exit when the pullback resolves (RSI back through 50) or the trend flips.
Conviction ``score`` is the pullback depth (a deeper dip ranks higher); exits carry no score.

Bar-driven, deterministic, look-ahead-clean (closes up to the current bar only). Tunables:
``config/strategies/trend_pullback.yaml``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from alpha_core.core.enums import OrderType, Side
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Signal, Tick
from alpha_core.helpers.config import load_yaml
from alpha_core.strategy.examples.indicators import rsi, sma


class TrendPullbackConfig(BaseModel):
    """Trend-pullback tunables (``config/strategies/trend_pullback.yaml``)."""

    model_config = ConfigDict(extra="forbid")
    strategy_id: str = "trend_pullback"
    trend_period: int = 50
    rsi_period: int = 14
    pullback_level: Decimal = Decimal("40")  # RSI dip (uptrend) / 100-level rally (downtrend)
    quantity: Decimal = Decimal("1")


@dataclass
class _State:
    closes: deque[Decimal]
    position: Side | None = None


class TrendPullback(Strategy):
    """Trend + RSI-pullback confluence (see module docstring)."""

    def __init__(self, config: TrendPullbackConfig | None = None) -> None:
        self._cfg = config or TrendPullbackConfig()
        if self._cfg.trend_period <= 0 or self._cfg.rsi_period <= 0:
            raise ValueError("trend_period and rsi_period must be positive")
        if not 0 < self._cfg.pullback_level < 50:
            raise ValueError("require 0 < pullback_level < 50")
        self._window = max(self._cfg.trend_period, self._cfg.rsi_period + 1) + 2
        self._state: dict[str, _State] = {}

    @classmethod
    def from_config(cls) -> TrendPullback:
        return cls(TrendPullbackConfig.model_validate(load_yaml("strategies/trend_pullback.yaml")))

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        st = self._state.get(bar.symbol)
        if st is None:
            st = _State(deque(maxlen=self._window))
            self._state[bar.symbol] = st
        st.closes.append(bar.close)
        closes = list(st.closes)
        trend = sma(closes, self._cfg.trend_period)
        r = rsi(closes, self._cfg.rsi_period)
        if trend is None or r is None:
            return []
        return self._react(bar, st, trend, r)

    def _react(self, bar: Bar, st: _State, trend: Decimal, r: Decimal) -> Sequence[Signal]:
        uptrend, downtrend = bar.close > trend, bar.close < trend
        level = self._cfg.pullback_level
        upper = Decimal(100) - level  # the downtrend's RSI-rally threshold (mirror of the dip)
        if st.position is None:
            if uptrend and r < level:
                st.position = Side.BUY
                return [self._signal(bar, Side.BUY, (level - r) / level, "uptrend + RSI pullback")]
            if downtrend and r > upper:
                st.position = Side.SELL
                return [self._signal(bar, Side.SELL, (r - upper) / level, "downtrend + RSI rally")]
            return []
        if st.position is Side.BUY and (r >= 50 or downtrend):
            st.position = None
            return [self._signal(bar, Side.SELL, None, "pullback recovered / trend flip")]
        if st.position is Side.SELL and (r <= 50 or uptrend):
            st.position = None
            return [self._signal(bar, Side.BUY, None, "rally faded / trend flip")]
        return []

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []

    def _signal(self, bar: Bar, side: Side, score: Decimal | None, reason: str) -> Signal:
        return Signal(
            strategy_id=self._cfg.strategy_id,
            symbol=bar.symbol,
            asset_class=bar.asset_class,
            side=side,
            quantity=self._cfg.quantity,
            order_type=OrderType.MARKET,
            created_at=bar.start + bar.interval,
            reason=reason,
            score=score,
        )
