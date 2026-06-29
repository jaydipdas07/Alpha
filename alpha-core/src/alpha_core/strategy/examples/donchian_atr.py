"""Donchian breakout with an ATR volatility-regime filter (richer templates, M3.0).

Trend-following breakout, but only when the market is actually moving: go long when the close
breaks above the prior ``channel_period``-bar high (short below the prior low) **and** volatility
(ATR / price) is at least ``atr_floor_bps`` — skipping low-volatility chop where breakouts
whipsaw. Exit a held position on the opposite channel (a volatility-trailed stop). Conviction
``score`` is the breakout distance in ATR units; exits carry no score.

Bar-driven, deterministic, look-ahead-clean: the channel uses only the **prior** bars (excludes the
current close), so a close can break it without peeking.
Tunables: ``config/strategies/donchian_atr.yaml``.
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
from alpha_core.strategy.examples.indicators import atr, donchian


class DonchianAtrConfig(BaseModel):
    """Donchian/ATR tunables (``config/strategies/donchian_atr.yaml``)."""

    model_config = ConfigDict(extra="forbid")
    strategy_id: str = "donchian_atr"
    channel_period: int = 20
    atr_period: int = 14
    atr_floor_bps: Decimal = Decimal("30")  # min ATR/price (bps) to take a breakout
    quantity: Decimal = Decimal("1")


@dataclass
class _State:
    highs: deque[Decimal]
    lows: deque[Decimal]
    closes: deque[Decimal]
    position: Side | None = None


class DonchianAtr(Strategy):
    """Donchian breakout + ATR regime filter (see module docstring)."""

    def __init__(self, config: DonchianAtrConfig | None = None) -> None:
        self._cfg = config or DonchianAtrConfig()
        if self._cfg.channel_period <= 0 or self._cfg.atr_period <= 0:
            raise ValueError("channel_period and atr_period must be positive")
        if self._cfg.atr_floor_bps < 0:
            raise ValueError("atr_floor_bps must be >= 0")
        self._window = max(self._cfg.channel_period, self._cfg.atr_period) + 2
        self._state: dict[str, _State] = {}

    @classmethod
    def from_config(cls) -> DonchianAtr:
        return cls(DonchianAtrConfig.model_validate(load_yaml("strategies/donchian_atr.yaml")))

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        st = self._state.get(bar.symbol)
        if st is None:
            w = self._window
            st = _State(deque(maxlen=w), deque(maxlen=w), deque(maxlen=w))
            self._state[bar.symbol] = st
        st.highs.append(bar.high)
        st.lows.append(bar.low)
        st.closes.append(bar.close)
        highs, lows, closes = list(st.highs), list(st.lows), list(st.closes)
        channel = donchian(highs[:-1], lows[:-1], self._cfg.channel_period)  # prior bars only
        a = atr(highs, lows, closes, self._cfg.atr_period)
        if channel is None or a is None or a == 0:
            return []
        return self._react(bar, st, channel[0], channel[1], a)

    def _react(
        self, bar: Bar, st: _State, upper: Decimal, lower: Decimal, a: Decimal
    ) -> Sequence[Signal]:
        if st.position is None:
            if a / bar.close < self._cfg.atr_floor_bps / Decimal(10000):
                return []  # volatility too low — skip the breakout
            if bar.close > upper:
                st.position = Side.BUY
                return [
                    self._signal(bar, Side.BUY, (bar.close - upper) / a, "Donchian breakout up")
                ]
            if bar.close < lower:
                st.position = Side.SELL
                return [
                    self._signal(bar, Side.SELL, (lower - bar.close) / a, "Donchian breakout dn")
                ]
            return []
        if st.position is Side.BUY and bar.close < lower:
            st.position = None
            return [self._signal(bar, Side.SELL, None, "below lower channel — exit long")]
        if st.position is Side.SELL and bar.close > upper:
            st.position = None
            return [self._signal(bar, Side.BUY, None, "above upper channel — exit short")]
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
