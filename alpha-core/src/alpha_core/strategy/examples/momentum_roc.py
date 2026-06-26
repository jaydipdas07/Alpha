"""Momentum / rate-of-change — trend-following (R7).

Go long when the ``period``-bar ROC pushes above ``+threshold_pct``, short when it
falls below ``-threshold_pct``; flip on the opposite signal. Conviction ``score``
is |ROC| / threshold, so a stronger thrust ranks higher. Bar-driven,
deterministic, look-ahead-clean (ROC uses only closes up to the current bar).
Tunables: ``config/strategies/momentum_roc.yaml`` (ADR 0011).
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
from alpha_core.strategy.examples.indicators import roc


class MomentumRocConfig(BaseModel):
    """Momentum/ROC tunables (``config/strategies/momentum_roc.yaml``)."""

    model_config = ConfigDict(extra="forbid")
    strategy_id: str = "momentum_roc"
    period: int = 12
    threshold_pct: Decimal = Decimal("2")
    quantity: Decimal = Decimal("1")


@dataclass
class _State:
    closes: deque[Decimal]
    position: Side | None = None


class MomentumRoc(Strategy):
    """Rate-of-change momentum (see module docstring)."""

    def __init__(self, config: MomentumRocConfig | None = None) -> None:
        self._cfg = config or MomentumRocConfig()
        if self._cfg.period <= 0:
            raise ValueError("period must be positive")
        if self._cfg.threshold_pct <= 0:
            raise ValueError("threshold_pct must be positive")
        self._state: dict[str, _State] = {}

    @classmethod
    def from_config(cls) -> MomentumRoc:
        return cls(MomentumRocConfig.model_validate(load_yaml("strategies/momentum_roc.yaml")))

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        st = self._state.get(bar.symbol)
        if st is None:
            st = _State(deque(maxlen=self._cfg.period + 1))
            self._state[bar.symbol] = st
        st.closes.append(bar.close)
        change = roc(list(st.closes), self._cfg.period)
        if change is None:
            return []
        threshold = self._cfg.threshold_pct
        score = abs(change) / threshold
        if change > threshold and st.position is not Side.BUY:
            st.position = Side.BUY
            return [self._signal(bar, Side.BUY, score, "momentum up")]
        if change < -threshold and st.position is not Side.SELL:
            st.position = Side.SELL
            return [self._signal(bar, Side.SELL, score, "momentum down")]
        return []

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []

    def _signal(self, bar: Bar, side: Side, score: Decimal, reason: str) -> Signal:
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
