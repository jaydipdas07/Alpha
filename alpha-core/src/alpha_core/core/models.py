"""Core domain models — immutable, money/time-safe (the B0.7 contract slice).

Conventions (enforced here, fail-fast):
- Money & price: :class:`~decimal.Decimal` in the quote currency per unit. Never
  float — float inputs are rejected (accept Decimal/int/str).
- Quantity: ``Decimal`` in instrument units. Never float.
- Time: timezone-aware ``datetime`` in UTC. Naive datetimes are rejected.
- ``Bar``/``Tick``/``Signal`` are immutable.

The full model set (``Order``/``Fill``/``Position`` + the order FSM) is lifted from
Vega in B0.9; this slice is only what the strategy contract needs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from alpha_core.core.enums import AssetClass, OrderType, Side, Venue


def _no_float(v: object) -> object:
    """Reject float inputs for money/quantity; accept Decimal/int/str."""
    if isinstance(v, float):
        raise ValueError("money/quantity must be Decimal, int, or str — never float")
    return v


def _to_utc(v: datetime) -> datetime:
    """Require a tz-aware datetime and normalize it to UTC."""
    if v.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware (UTC)")
    return v.astimezone(UTC)


# Annotated field types (the no-float / UTC discipline, reused across models).
PosMoney = Annotated[Decimal, BeforeValidator(_no_float), Field(gt=0)]
PosQty = Annotated[Decimal, BeforeValidator(_no_float), Field(gt=0)]
NonNegQty = Annotated[Decimal, BeforeValidator(_no_float), Field(ge=0)]
# Conviction magnitude for cross-sectional ranking (direction lives in ``side``).
Score = Annotated[Decimal, BeforeValidator(_no_float), Field(ge=0)]
UtcDatetime = Annotated[datetime, AfterValidator(_to_utc)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Bar(_Frozen):
    """OHLCV bar covering ``[start, start + interval)`` (immutable)."""

    symbol: str
    venue: Venue
    asset_class: AssetClass
    start: UtcDatetime
    interval: timedelta
    open: PosMoney
    high: PosMoney
    low: PosMoney
    close: PosMoney
    volume: NonNegQty

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.interval <= timedelta(0):
            raise ValueError("interval must be positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be >= open, close, low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be <= open, close, high")
        return self


class Tick(_Frozen):
    """A market tick (immutable). Minimal B0.7 slice: the last trade price."""

    symbol: str
    venue: Venue
    asset_class: AssetClass
    ts: UtcDatetime
    last_price: PosMoney


class Signal(_Frozen):
    """Abstract order intent emitted by a strategy (immutable).

    Direction is ``side``; ``score`` is an optional conviction magnitude for
    cross-sectional ranking (``None`` = no conviction expressed). The B0.7 contract
    is MARKET-oriented; limit/stop prices + the order FSM arrive with the lift (B0.9).
    """

    strategy_id: str
    symbol: str
    asset_class: AssetClass
    side: Side
    quantity: PosQty
    created_at: UtcDatetime
    order_type: OrderType = OrderType.MARKET
    reason: str | None = None
    score: Score | None = None
