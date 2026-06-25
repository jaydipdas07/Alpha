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

from pydantic import BaseModel, ConfigDict, Field

from alpha_core.core.models import Position


class FundingConfig(BaseModel):
    """Perp funding parameters (the ``funding.crypto`` block of ``config/costs.yaml``)."""

    model_config = ConfigDict(extra="forbid")
    interval_hours: int = Field(default=8, gt=0)  # Binance USDⓈ-M perps fund every 8h
    rate: Decimal = Field(default=Decimal("0.0001"))  # per-interval rate (assumed constant)


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
