"""MACD momentum (richer templates, M3.0).

EMA-based momentum: the MACD line (EMA(fast) - EMA(slow)) crossing its signal line (an EMA of the
MACD line) flags a momentum shift. Go long on a bullish cross (the histogram crosses up through
zero), short on a bearish cross, and exit a held position on the opposite cross. Conviction
``score`` is the histogram magnitude as a fraction of price (a wider gap = a stronger shift);
exits carry no score.

Bar-driven, deterministic, look-ahead-clean (closes up to the current bar only). Tunables:
``config/strategies/macd.yaml``.
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
from alpha_core.strategy.examples.indicators import macd


class MacdConfig(BaseModel):
    """MACD tunables (``config/strategies/macd.yaml``)."""

    model_config = ConfigDict(extra="forbid")
    strategy_id: str = "macd"
    fast_period: int = 12
    slow_period: int = 26
    signal_period: int = 9
    quantity: Decimal = Decimal("1")


@dataclass
class _State:
    closes: deque[Decimal]
    position: Side | None = None
    prev_hist: Decimal | None = None


class Macd(Strategy):
    """MACD momentum (see module docstring)."""

    def __init__(self, config: MacdConfig | None = None) -> None:
        self._cfg = config or MacdConfig()
        if not 0 < self._cfg.fast_period < self._cfg.slow_period or self._cfg.signal_period <= 0:
            raise ValueError("require 0 < fast_period < slow_period and signal_period > 0")
        # the recursive EMA is recomputed over a sliding window each bar, so the burn-in scales with
        # slow_period (~5 time-constants) for the SMA-seeded EMAs to converge before the cross is read
        self._window = self._cfg.slow_period * 5 + self._cfg.signal_period
        self._state: dict[str, _State] = {}

    @classmethod
    def from_config(cls) -> Macd:
        return cls(MacdConfig.model_validate(load_yaml("strategies/macd.yaml")))

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        st = self._state.get(bar.symbol)
        if st is None:
            st = _State(deque(maxlen=self._window))
            self._state[bar.symbol] = st
        st.closes.append(bar.close)
        result = macd(
            list(st.closes), self._cfg.fast_period, self._cfg.slow_period, self._cfg.signal_period
        )
        if result is None:
            return []
        _line, _signal, hist = result
        prev, st.prev_hist = st.prev_hist, hist
        if prev is None:
            return []  # need a prior histogram to detect a cross
        return self._react(bar, st, prev, hist)

    def _react(self, bar: Bar, st: _State, prev: Decimal, hist: Decimal) -> Sequence[Signal]:
        bullish = prev <= 0 < hist  # histogram crossed up through zero (line above signal)
        bearish = prev >= 0 > hist  # crossed down
        score = abs(hist) / bar.close  # conviction: histogram as a fraction of price
        if st.position is None:
            if bullish:
                st.position = Side.BUY
                return [self._signal(bar, Side.BUY, score, "MACD bullish cross")]
            if bearish:
                st.position = Side.SELL
                return [self._signal(bar, Side.SELL, score, "MACD bearish cross")]
            return []
        if st.position is Side.BUY and bearish:
            st.position = None
            return [self._signal(bar, Side.SELL, None, "MACD bearish cross — exit long")]
        if st.position is Side.SELL and bullish:
            st.position = None
            return [self._signal(bar, Side.BUY, None, "MACD bullish cross — exit short")]
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
