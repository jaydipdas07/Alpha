"""Config loading — the single source of truth for tunables (CLAUDE.md).

No magic numbers in code: every business parameter lives in ``config/``, loaded here
and validated by a pydantic model at the call site. The config dir is the repo-root
``config/``, overridable via ``ALPHA_CONFIG_DIR`` (tests / deployment). The full
layered loader (per-env configs, the live-mode gate, secret scanning) is lifted from
Vega in B0.9.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConfigError(Exception):
    """Raised when configuration is missing or invalid (fail fast)."""


def _config_dir() -> Path:
    """Resolve the active config directory (``ALPHA_CONFIG_DIR`` or the repo root)."""
    override = os.environ.get("ALPHA_CONFIG_DIR")
    if override:
        return Path(override)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config"
        if candidate.is_dir():
            return candidate
    raise ConfigError(  # pragma: no cover - defensive; config/ sits at the repo root
        "could not locate the config/ directory; set ALPHA_CONFIG_DIR"
    )


def load_yaml(name: str) -> dict[str, Any]:
    """Load and parse ``config/<name>`` as a mapping (honors ``ALPHA_CONFIG_DIR``)."""
    path = _config_dir() / name
    if not path.is_file():
        raise ConfigError(f"missing config file: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"config {name} must be a mapping at top level")
    return raw


# --- Portfolio allocator config (lifted from Vega config.py for B0.9d backtest) ----
# The pure-allocator parameters (portfolio.yaml). The fuller layered loader (per-env
# configs, the live-mode gate, secret scanning, universe config) is still pending its
# own increment; only the portfolio models the backtest runner needs are lifted here.
class AllocationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["equal_weight", "inverse_vol"] = "equal_weight"
    top_k: int = Field(gt=0)
    max_weight_per_name: Decimal = Field(gt=0, le=1)
    gross_cap: Decimal = Field(gt=0)


class RebalanceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    no_trade_band: Decimal = Field(ge=0)
    turnover_cap: Decimal = Field(gt=0)
    # Capital-fragmentation floor as a *fraction of capital* (a weight), so it
    # auto-rescales with base_capital (ADR 0006 — one knob). The configured book
    # must be able to fund every name at this weight; PortfolioConfig enforces it.
    min_position_weight: Decimal = Field(ge=0, le=1)


class PortfolioConfig(BaseModel):
    """``portfolio.yaml`` — the pure-allocator parameters (scaling design D6).
    The risk overlay (``risk.yaml``) caps the allocator's output separately."""

    model_config = ConfigDict(extra="forbid")
    allocation: AllocationConfig
    rebalance: RebalanceConfig

    @model_validator(mode="after")
    def _check_book_is_fundable(self) -> Self:
        """Fail fast on a config the allocator could never fund (CONFIG-1).

        An equal-weight top-K book caps each name at ``min(1/top_k,
        max_weight_per_name)`` of capital. If that is below the fragmentation
        floor, *every* name is below the floor on every rebalance — the engine
        would silently trade nothing. Reject it at load rather than discover it
        as an empty book at runtime.
        """
        a = self.allocation
        max_name_weight = min(Decimal(1) / a.top_k, a.max_weight_per_name)
        floor = self.rebalance.min_position_weight
        if max_name_weight < floor:
            raise ValueError(
                f"unfundable portfolio: a top_k={a.top_k} equal-weight book caps "
                f"each name at {max_name_weight} of capital, below "
                f"min_position_weight={floor} — the book would always be empty. "
                "Lower min_position_weight, raise max_weight_per_name, or reduce top_k."
            )
        return self


def load_portfolio_config() -> PortfolioConfig:
    """Load + validate ``config/portfolio.yaml`` (honors ``ALPHA_CONFIG_DIR``)."""
    return PortfolioConfig.model_validate(load_yaml("portfolio.yaml"))
