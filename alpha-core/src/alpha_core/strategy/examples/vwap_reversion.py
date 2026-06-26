"""VWAP reversion — intraday mean-reversion to the session VWAP (R7).

Within each trading day it tracks the volume-weighted average price (cumulative
typical-price x volume / cumulative volume). When the close stretches more than
``band_bps`` above VWAP it shorts (expecting reversion down); more than that below,
it buys. The position is closed when price crosses back through VWAP. Conviction
``score`` is the fractional distance |close - vwap| / vwap. The VWAP resets each
day (inferred from ``bar.start`` date — no clock), so backtest and live agree.

Bar-driven, deterministic, look-ahead-clean. Tunables:
``config/strategies/vwap_reversion.yaml`` (ADR 0011).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from alpha_core.core.enums import OrderType, Side
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Signal, Tick
from alpha_core.helpers.config import load_yaml

_BPS = Decimal(10_000)


class VwapReversionConfig(BaseModel):
    """VWAP-reversion tunables (``config/strategies/vwap_reversion.yaml``)."""

    model_config = ConfigDict(extra="forbid")
    strategy_id: str = "vwap_reversion"
    band_bps: Decimal = Decimal("50")  # distance from VWAP to act (basis points)
    quantity: Decimal = Decimal("1")


@dataclass
class _State:
    day: date
    cum_pv: Decimal = Decimal(0)  # Σ typical_price x volume
    cum_vol: Decimal = Decimal(0)  # Σ volume
    position: Side | None = None


class VwapReversion(Strategy):
    """Intraday VWAP reversion (see module docstring)."""

    def __init__(self, config: VwapReversionConfig | None = None) -> None:
        self._cfg = config or VwapReversionConfig()
        if self._cfg.band_bps <= 0:
            raise ValueError("band_bps must be positive")
        self._state: dict[str, _State] = {}

    @classmethod
    def from_config(cls) -> VwapReversion:
        return cls(VwapReversionConfig.model_validate(load_yaml("strategies/vwap_reversion.yaml")))

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        st = self._state.get(bar.symbol)
        if st is None or bar.start.date() != st.day:
            st = _State(day=bar.start.date())
            self._state[bar.symbol] = st
        typical = (bar.high + bar.low + bar.close) / Decimal(3)
        st.cum_pv += typical * bar.volume
        st.cum_vol += bar.volume
        if st.cum_vol == 0:
            return []
        vwap = st.cum_pv / st.cum_vol
        band = self._cfg.band_bps / _BPS
        score = abs(bar.close - vwap) / vwap
        if st.position is None:
            if bar.close > vwap * (Decimal(1) + band):
                st.position = Side.SELL
                return [self._signal(bar, Side.SELL, score, "stretched above VWAP")]
            if bar.close < vwap * (Decimal(1) - band):
                st.position = Side.BUY
                return [self._signal(bar, Side.BUY, score, "stretched below VWAP")]
            return []
        if st.position is Side.SELL and bar.close <= vwap:
            st.position = None
            return [self._signal(bar, Side.BUY, None, "reverted to VWAP")]
        if st.position is Side.BUY and bar.close >= vwap:
            st.position = None
            return [self._signal(bar, Side.SELL, None, "reverted to VWAP")]
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
