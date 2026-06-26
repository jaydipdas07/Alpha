"""RSI + Bollinger mean-reversion (R7).

Fade extremes: go long when RSI is oversold *and* the close is below the lower
Bollinger band; go short when RSI is overbought *and* the close is above the upper
band. Exit when price reverts to the band mid (the SMA). Conviction ``score`` is
the RSI extremity beyond the threshold, so a deeper extreme ranks higher. Exits
carry no score (they are unconditional risk reduction, not a fresh bet).

Bar-driven, deterministic, look-ahead-clean (uses only closes up to the current
bar). Tunables: ``config/strategies/rsi_bollinger.yaml`` (ADR 0011).
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
from alpha_core.strategy.examples.indicators import bollinger, rsi


class RsiBollingerConfig(BaseModel):
    """RSI/Bollinger tunables (``config/strategies/rsi_bollinger.yaml``)."""

    model_config = ConfigDict(extra="forbid")
    strategy_id: str = "rsi_bollinger"
    rsi_period: int = 14
    oversold: Decimal = Decimal("30")
    overbought: Decimal = Decimal("70")
    bollinger_period: int = 20
    num_std: Decimal = Decimal("2")
    quantity: Decimal = Decimal("1")


@dataclass
class _State:
    closes: deque[Decimal]
    position: Side | None = None


class RsiBollinger(Strategy):
    """RSI + Bollinger mean-reversion (see module docstring)."""

    def __init__(self, config: RsiBollingerConfig | None = None) -> None:
        self._cfg = config or RsiBollingerConfig()
        if self._cfg.rsi_period <= 0 or self._cfg.bollinger_period <= 0:
            raise ValueError("periods must be positive")
        if not 0 < self._cfg.oversold < self._cfg.overbought < 100:
            raise ValueError("require 0 < oversold < overbought < 100")
        self._window = max(self._cfg.rsi_period + 1, self._cfg.bollinger_period)
        self._state: dict[str, _State] = {}

    @classmethod
    def from_config(cls) -> RsiBollinger:
        return cls(RsiBollingerConfig.model_validate(load_yaml("strategies/rsi_bollinger.yaml")))

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        st = self._state.get(bar.symbol)
        if st is None:
            st = _State(deque(maxlen=self._window))
            self._state[bar.symbol] = st
        st.closes.append(bar.close)
        closes = list(st.closes)
        r = rsi(closes, self._cfg.rsi_period)
        bands = bollinger(closes, self._cfg.bollinger_period, self._cfg.num_std)
        if r is None or bands is None:
            return []
        mid, upper, lower = bands
        if st.position is None:
            return self._maybe_enter(bar, st, r, upper, lower)
        return self._maybe_exit(bar, st, mid)

    def _maybe_enter(
        self, bar: Bar, st: _State, r: Decimal, upper: Decimal, lower: Decimal
    ) -> Sequence[Signal]:
        if r < self._cfg.oversold and bar.close < lower:
            st.position = Side.BUY
            score = (self._cfg.oversold - r) / self._cfg.oversold
            return [self._signal(bar, Side.BUY, score, "RSI oversold + below lower band")]
        if r > self._cfg.overbought and bar.close > upper:
            st.position = Side.SELL
            score = (r - self._cfg.overbought) / (Decimal(100) - self._cfg.overbought)
            return [self._signal(bar, Side.SELL, score, "RSI overbought + above upper band")]
        return []

    def _maybe_exit(self, bar: Bar, st: _State, mid: Decimal) -> Sequence[Signal]:
        if st.position is Side.BUY and bar.close >= mid:
            st.position = None
            return [self._signal(bar, Side.SELL, None, "revert to band mid")]
        if st.position is Side.SELL and bar.close <= mid:
            st.position = None
            return [self._signal(bar, Side.BUY, None, "revert to band mid")]
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
