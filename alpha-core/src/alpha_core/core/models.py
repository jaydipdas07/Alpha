"""Core domain models — exactly as fixed by ADR 0002.

Conventions (enforced here, fail-fast):
- Money & price: ``Decimal`` in the instrument's quote currency per unit. Never
  float — float inputs are rejected (accept Decimal/int/str).
- Quantity: ``Decimal`` in instrument units. Never float.
- Time: timezone-aware ``datetime`` in UTC. Naive datetimes are rejected.
- ``Bar``/``Tick``/``Fill``/``Signal`` are immutable; ``Order``/``Position`` are
  mutable aggregates changed only by the FSM (ADR 0003) / OMS.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from alpha_core.core.enums import (
    AssetClass,
    OptionRight,
    OrderState,
    OrderType,
    Settlement,
    Side,
    Venue,
)


def _no_float(v: object) -> object:
    """Reject float inputs for money/quantity (ADR 0002); accept Decimal/int/str."""
    if isinstance(v, float):
        raise ValueError("money/quantity must be Decimal, int, or str — never float")
    return v


def _to_utc(v: datetime) -> datetime:
    """Require a tz-aware datetime and normalize it to UTC (ADR 0002)."""
    if v.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware (UTC)")
    return v.astimezone(UTC)


# Annotated field types.
Money = Annotated[Decimal, BeforeValidator(_no_float)]
PosMoney = Annotated[Decimal, BeforeValidator(_no_float), Field(gt=0)]
NonNegMoney = Annotated[Decimal, BeforeValidator(_no_float), Field(ge=0)]
PosQty = Annotated[Decimal, BeforeValidator(_no_float), Field(gt=0)]
NonNegQty = Annotated[Decimal, BeforeValidator(_no_float), Field(ge=0)]
SignedQty = Annotated[Decimal, BeforeValidator(_no_float)]
UtcDatetime = Annotated[datetime, AfterValidator(_to_utc)]
# Dimensionless Decimal scalars (no float, same discipline as money/qty).
# Score: a strategy's conviction magnitude (direction lives in ``side``).
# Weight: a signed fraction of deployable capital (>0 long, <0 short).
Score = Annotated[Decimal, BeforeValidator(_no_float), Field(ge=0)]
Weight = Annotated[Decimal, BeforeValidator(_no_float)]
# A Greek sensitivity (signed: a put's delta/theta are negative). Same no-float
# discipline; ``None`` means "uncomputable" (no IV) — never silently treated as 0.
Greek = Annotated[Decimal, BeforeValidator(_no_float)]


def _check_price_presence(
    order_type: OrderType, limit_price: Decimal | None, stop_price: Decimal | None
) -> None:
    """limit/stop price required iff the order type needs it; else must be absent."""
    needs_limit = order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT)
    needs_stop = order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
    if needs_limit and limit_price is None:
        raise ValueError(f"{order_type} requires limit_price")
    if not needs_limit and limit_price is not None:
        raise ValueError(f"{order_type} must not carry a limit_price")
    if needs_stop and stop_price is None:
        raise ValueError(f"{order_type} requires stop_price")
    if not needs_stop and stop_price is not None:
        raise ValueError(f"{order_type} must not carry a stop_price")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _Mutable(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class OptionContract(_Frozen):
    """Parsed option-contract metadata for an `INDEX_OPTION` (or crypto option),
    carried on `InstrumentSpec`/`InstrumentMeta` (ADR 0017). The *canonical symbol*
    stays the opaque venue-qualified string (e.g. ``NFO:NIFTY24JUL24000CE``); this is
    the structured view the adapter/instruments layer derives from the venue's
    instrument master — the core never parses a venue string itself."""

    underlying: str  # e.g. "NIFTY", "BANKNIFTY", "BTC"
    right: OptionRight
    strike: PosMoney
    expiry: date
    lot_size: int = Field(gt=0)
    settlement: Settlement = Settlement.CASH
    multiplier: PosMoney = Decimal(1)  # contract multiplier (1 for NSE index options)


class OptionGreeks(_Frozen):
    """Derived option sensitivities (ADR 0017) — **analytics, not a trade intent**, so
    they live on a quote/position view, never on the immutable `Signal`/`Order`. Raw
    units: vega per 1.00 vol, theta per year, rho per 1.00 rate (downstream scales).
    Any field is ``None`` when uncomputable (no implied vol) — never silently 0."""

    delta: Greek | None = None
    gamma: Greek | None = None
    vega: Greek | None = None
    theta: Greek | None = None
    rho: Greek | None = None
    iv: Greek | None = None  # implied volatility solved from the mid premium


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
    """A market tick (immutable). Needs ``last_price`` or a full bid/ask pair."""

    symbol: str
    venue: Venue
    asset_class: AssetClass
    ts: UtcDatetime
    last_price: PosMoney | None = None
    last_qty: PosQty | None = None
    bid: PosMoney | None = None
    ask: PosMoney | None = None
    bid_qty: PosQty | None = None
    ask_qty: PosQty | None = None
    volume: NonNegQty | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.last_price is None and (self.bid is None or self.ask is None):
            raise ValueError("tick needs last_price or both bid and ask")
        return self


class Order(_Mutable):
    """A working order (mutable; transitions governed by the FSM, ADR 0003)."""

    client_order_id: str
    venue_order_id: str | None = None
    symbol: str
    venue: Venue
    asset_class: AssetClass
    side: Side
    order_type: OrderType
    quantity: PosQty
    limit_price: PosMoney | None = None
    stop_price: PosMoney | None = None
    state: OrderState
    filled_quantity: NonNegQty = Decimal("0")
    average_fill_price: PosMoney | None = None
    strategy_id: str
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_price_presence(self.order_type, self.limit_price, self.stop_price)
        if self.filled_quantity > self.quantity:
            raise ValueError("filled_quantity must be <= quantity")
        if (self.filled_quantity == 0) != (self.average_fill_price is None):
            raise ValueError("average_fill_price is set iff filled_quantity > 0")
        return self


class Fill(_Frozen):
    """A single execution (immutable, append-only)."""

    fill_id: str
    client_order_id: str
    venue_order_id: str | None = None
    venue_fill_id: str | None = None
    symbol: str
    venue: Venue
    asset_class: AssetClass
    side: Side
    quantity: PosQty
    price: PosMoney
    fees: NonNegMoney | None = None
    ts: UtcDatetime


class Position(_Mutable):
    """Net position per ``(venue, symbol)`` (mutable). Quantity is signed."""

    symbol: str
    venue: Venue
    asset_class: AssetClass
    quantity: SignedQty
    average_price: PosMoney | None = None
    realized_pnl: Money = Decimal("0")
    unrealized_pnl: Money | None = None
    last_price: PosMoney | None = None
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.quantity == 0) != (self.average_price is None):
            raise ValueError("average_price is set iff quantity != 0")
        return self


class Signal(_Frozen):
    """Abstract order intent emitted by a strategy (immutable, ADR 0002)."""

    strategy_id: str
    symbol: str
    asset_class: AssetClass
    side: Side
    quantity: PosQty
    order_type: OrderType
    limit_price: PosMoney | None = None
    stop_price: PosMoney | None = None
    created_at: UtcDatetime
    valid_until: UtcDatetime | None = None
    reason: str | None = None
    # Conviction for cross-sectional ranking (scaling design D8). Additive and
    # optional: ``None`` = no conviction expressed (un-ranked / equal baseline).
    # Magnitude only — direction is ``side``; higher = stronger.
    score: Score | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_price_presence(self.order_type, self.limit_price, self.stop_price)
        return self


class TargetExposure(_Frozen):
    """A desired exposure in one instrument as a signed fraction of deployable
    capital (immutable; scaling design PB/D2). The portfolio engine diffs
    current→target to produce orders. Sign is direction (>0 long, <0 short,
    0 = flat); magnitude is the capital fraction. Per-name / gross / correlation
    caps are enforced by the risk overlay, not by this type.

    Keyed on ``symbol`` (venue-qualified, like ``Signal``) — the venue-agnostic
    core never carries a venue here; the executor resolves symbol→venue."""

    strategy_id: str
    symbol: str
    asset_class: AssetClass
    weight: Weight
