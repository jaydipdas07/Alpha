"""Perp funding accrual (R13) — the periodic funding cash flow on a held perp.

Perpetual futures charge a **funding payment** every interval (e.g. 8h on Binance):
longs pay shorts when the rate is positive, shorts pay longs when it's negative. A
position held through many intervals **bleeds funding** even with no price move — so
funding accrues into P&L AND feeds the daily-loss kill gate (a funding-bleed can trip
it). The rate is a configured constant for now (real per-interval rates come from the
funding-rate data feed later); the accrual mechanics are what R13 adds.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from alpha_core.core.models import Position


class FundingConfig(BaseModel):
    """Perp funding parameters (the ``funding.crypto`` block of ``config/costs.yaml``).

    ``interval_hours`` MUST match the venue's per-symbol funding interval — accrual
    boundaries are UTC-midnight-anchored every ``interval_hours``, and a mismatch
    mis-accrues silently (config 8h on a venue funding 4h books half the true funding).
    ``rate`` is only the FALLBACK when the venue cannot answer (the worker's accrual
    truth is ``BrokerAdapter.funding_rate``)."""

    model_config = ConfigDict(extra="forbid")
    interval_hours: int = Field(default=8, gt=0, le=24)  # Binance USDⓈ-M perps fund every 8h
    rate: Decimal = Field(default=Decimal("0.0001"))  # per-interval FALLBACK rate

    @field_validator("interval_hours")
    @classmethod
    def _divides_a_day(cls, value: int) -> int:
        """Boundaries are UTC-midnight-anchored: a non-divisor of 24 (e.g. 7h) would drift
        across days and accrue uneven intervals — reject at load, not at the boundary."""
        if 24 % value != 0:
            raise ValueError(f"interval_hours must divide 24 evenly; got {value}")
        return value


def funding_cash_flow(position: Position, mark_price: Decimal, rate: Decimal) -> Decimal:
    """One interval's funding cash flow for ``position`` at ``mark_price``.

    Long (``quantity > 0``) **pays** when ``rate > 0`` (a negative cash flow); short
    receives; a flat position is zero. Cash flow = ``-quantity * notional_price * rate``.
    """
    return -position.quantity * mark_price * rate


def load_funding_config(cost_config: dict[str, Any]) -> FundingConfig | None:
    """Parse ``funding.crypto`` from a parsed ``costs.yaml`` mapping (``None`` if absent)."""
    raw = (cost_config.get("funding") or {}).get("crypto")
    return FundingConfig.model_validate(raw) if raw else None
