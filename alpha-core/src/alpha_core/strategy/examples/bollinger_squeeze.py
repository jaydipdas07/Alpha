"""Bollinger squeeze breakout (richer templates, M3.0).

A volatility-regime breakout: when the Bollinger bands are *narrow* — band-width
``(upper - lower) / mid`` at or below ``squeeze_bps`` (a low-volatility "squeeze") — a close
breaking the upper band signals a bullish volatility-expansion (lower band → bearish). Ride it,
then exit when price reverts back through the band mid. Conviction ``score`` is the breakout
distance past the band as a fraction of price; exits carry no score.

Bar-driven, deterministic, look-ahead-clean (closes up to the current bar only). Tunables:
``config/strategies/bollinger_squeeze.yaml``.
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
from alpha_core.strategy.examples.indicators import bollinger


class BollingerSqueezeConfig(BaseModel):
    """Bollinger-squeeze tunables (``config/strategies/bollinger_squeeze.yaml``)."""

    model_config = ConfigDict(extra="forbid")
    strategy_id: str = "bollinger_squeeze"
    period: int = 20
    num_std: Decimal = Decimal("2")
    squeeze_bps: Decimal = Decimal("150")  # max band-width (bps of price) that counts as a squeeze
    quantity: Decimal = Decimal("1")


@dataclass
class _State:
    closes: deque[Decimal]
    position: Side | None = None


class BollingerSqueeze(Strategy):
    """Bollinger squeeze breakout (see module docstring)."""

    def __init__(self, config: BollingerSqueezeConfig | None = None) -> None:
        self._cfg = config or BollingerSqueezeConfig()
        if self._cfg.period <= 0 or self._cfg.num_std <= 0:
            raise ValueError("period and num_std must be positive")
        if self._cfg.squeeze_bps <= 0:
            raise ValueError("squeeze_bps must be positive")
        self._window = self._cfg.period + 5
        self._state: dict[str, _State] = {}

    @classmethod
    def from_config(cls) -> BollingerSqueeze:
        return cls(
            BollingerSqueezeConfig.model_validate(load_yaml("strategies/bollinger_squeeze.yaml"))
        )

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        st = self._state.get(bar.symbol)
        if st is None:
            st = _State(deque(maxlen=self._window))
            self._state[bar.symbol] = st
        st.closes.append(bar.close)
        bands = bollinger(list(st.closes), self._cfg.period, self._cfg.num_std)
        if bands is None:
            return []
        mid, upper, lower = bands
        if mid == 0:
            return []
        return self._react(bar, st, mid, upper, lower)

    def _react(
        self, bar: Bar, st: _State, mid: Decimal, upper: Decimal, lower: Decimal
    ) -> Sequence[Signal]:
        if st.position is None:
            squeezed = (upper - lower) / mid <= self._cfg.squeeze_bps / Decimal(10000)
            if not squeezed:
                return []
            if bar.close > upper:
                st.position = Side.BUY
                return [
                    self._signal(bar, Side.BUY, (bar.close - upper) / mid, "squeeze breakout up")
                ]
            if bar.close < lower:
                st.position = Side.SELL
                return [
                    self._signal(bar, Side.SELL, (lower - bar.close) / mid, "squeeze breakout dn")
                ]
            return []
        if st.position is Side.BUY and bar.close <= mid:
            st.position = None
            return [self._signal(bar, Side.SELL, None, "revert to mid — exit long")]
        if st.position is Side.SELL and bar.close >= mid:
            st.position = None
            return [self._signal(bar, Side.BUY, None, "revert to mid — exit short")]
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
