"""Risk limits — typed load of ``config/risk.yaml`` (ADR 0006).

All values are % of a single ``base_capital`` (change it alone to rescale). No
risk number is hard-coded anywhere else.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from alpha_core.helpers.config import load_yaml


def _dec(v: object) -> Decimal:
    """Config numbers parse as float/int; convert exactly via str."""
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_gross_exposure: Decimal
    max_position_per_instrument: Decimal
    max_concurrent_positions: int
    max_order_value: Decimal
    max_orders_per_minute: int
    max_daily_loss_halt: Decimal
    max_loss_per_trade: Decimal
    per_segment_exposure_cap: Decimal


class KillSwitchTriggers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    daily_loss_breach: bool = True
    consecutive_errors: int = 3
    feed_stale_seconds: int = 5
    reconciliation_mismatch: bool = True
    manual: bool = True


class KillSwitchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    triggers: KillSwitchTriggers = Field(default_factory=KillSwitchTriggers)
    on_trip: dict[str, bool] = Field(default_factory=dict)
    rearm: str = "manual"


def _default_leverage() -> dict[str, Decimal]:
    # required margin = notional / leverage. Conservative pilot defaults; the
    # real F&O figure is SPAN + exposure (this is an estimate, refined live).
    return {"equity": Decimal("5"), "index_option": Decimal("8"), "crypto": Decimal("3")}


class MarginConfig(BaseModel):
    """Pre-trade margin estimate (ADR 0006 step 9). Keyed by asset-class name."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    leverage: dict[str, Decimal] = Field(default_factory=_default_leverage)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if any(lev <= 0 for lev in self.leverage.values()):
            raise ValueError("margin leverage must be positive")
        return self


class PortfolioLimits(BaseModel):
    """Portfolio-level risk caps applied to the allocator's *target weights* (the
    Risk overlay stage, scaling design D9). Non-bypassable: every rebalance target
    is clamped to these before it becomes orders, independent of the allocator's
    own (``portfolio.yaml``) params — defence in depth."""

    model_config = ConfigDict(extra="forbid")
    max_gross_weight: Decimal = Field(gt=0)  # ceiling on sum |weight|
    max_weight_per_name: Decimal = Field(gt=0, le=1)
    max_weight_per_sector: Decimal = Field(gt=0)  # ceiling on sum |weight| per sector
    # Options Greeks caps (ADR 0017): ceilings on the book's |net delta| / |net vega|.
    # Optional — unset (None) ⇒ no Greeks cap (equity/crypto books are unaffected).
    max_net_delta: Decimal | None = Field(default=None, gt=0)
    max_net_vega: Decimal | None = Field(default=None, gt=0)


class RiskConfig(BaseModel):
    """The whole ``risk.yaml`` (ADR 0006)."""

    model_config = ConfigDict(extra="forbid")
    base_capital: Decimal
    currency: str = "INR"
    limits: Limits
    kill_switch: KillSwitchConfig = Field(default_factory=KillSwitchConfig)
    margin: MarginConfig = Field(default_factory=MarginConfig)
    portfolio: PortfolioLimits | None = None  # the portfolio-risk overlay (P3)

    # absolute (₹) caps derived from base_capital — computed once
    def cap(self, fraction: Decimal) -> Decimal:
        return self.base_capital * fraction


def load_risk_config(name: str = "risk.yaml") -> RiskConfig:
    """Load + validate ``config/risk.yaml`` (fail fast on missing/invalid)."""
    raw = load_yaml(name)
    raw["base_capital"] = _dec(raw["base_capital"])
    limits = raw.get("limits", {})
    for key in (
        "max_gross_exposure",
        "max_position_per_instrument",
        "max_order_value",
        "max_daily_loss_halt",
        "max_loss_per_trade",
        "per_segment_exposure_cap",
    ):
        if key in limits:
            limits[key] = _dec(limits[key])
    margin = raw.get("margin")
    if isinstance(margin, dict) and isinstance(margin.get("leverage"), dict):
        margin["leverage"] = {k: _dec(v) for k, v in margin["leverage"].items()}
    cfg = RiskConfig.model_validate(raw)  # MarginConfig validates leverage > 0
    if cfg.base_capital <= 0:
        raise ValueError("base_capital must be positive")
    return cfg
