"""Moving-average crossover — a trivial, look-ahead-clean reference strategy.

Long when the fast SMA crosses above the slow SMA, short when it crosses below.
``score`` is the normalized gap ``|fast - slow| / slow`` so a strong separation ranks
above a marginal one. Bar-driven and deterministic: each decision uses only closes up
to the current bar — no wall-clock, no future data — so backtest ≡ live. All tunables
live in ``config/strategies/ma_crossover.yaml`` (no magic numbers).
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
from alpha_core.strategy.examples.indicators import sma


class MaCrossoverConfig(BaseModel):
    """MA-crossover tunables (``config/strategies/ma_crossover.yaml``)."""

    model_config = ConfigDict(extra="forbid")
    strategy_id: str = "ma_crossover"
    fast_period: int = 10
    slow_period: int = 30
    quantity: Decimal = Decimal("1")


@dataclass
class _State:
    """Per-symbol rolling state (look-ahead-clean: only past closes)."""

    closes: deque[Decimal]
    position: Side | None = None
    prev_diff: Decimal | None = None  # sign of (fast - slow) on the previous bar


class MaCrossover(Strategy):
    """Fast/slow SMA crossover (see module docstring)."""

    def __init__(self, config: MaCrossoverConfig | None = None) -> None:
        self._cfg = config or MaCrossoverConfig()
        if not 0 < self._cfg.fast_period < self._cfg.slow_period:
            raise ValueError("require 0 < fast_period < slow_period")
        self._state: dict[str, _State] = {}

    @classmethod
    def from_config(cls) -> MaCrossover:
        """Build from ``config/strategies/ma_crossover.yaml`` (no magic numbers)."""
        return cls(MaCrossoverConfig.model_validate(load_yaml("strategies/ma_crossover.yaml")))

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        st = self._state.get(bar.symbol)
        if st is None:
            st = _State(deque(maxlen=self._cfg.slow_period))
            self._state[bar.symbol] = st
        st.closes.append(bar.close)
        closes = list(st.closes)
        fast = sma(closes, self._cfg.fast_period)
        slow = sma(closes, self._cfg.slow_period)
        if fast is None or slow is None or slow == 0:
            return []
        diff = fast - slow
        signals: list[Signal] = []
        if st.prev_diff is not None:
            score = abs(diff) / slow
            if st.prev_diff <= 0 < diff and st.position is not Side.BUY:
                st.position = Side.BUY
                signals = [self._signal(bar, Side.BUY, score, "MA cross up")]
            elif st.prev_diff >= 0 > diff and st.position is not Side.SELL:
                st.position = Side.SELL
                signals = [self._signal(bar, Side.SELL, score, "MA cross down")]
        st.prev_diff = diff
        return signals

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []

    def _signal(self, bar: Bar, side: Side, score: Decimal, reason: str) -> Signal:
        # created_at is bar-time (start + interval), never wall-clock — backtest ≡ live.
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
