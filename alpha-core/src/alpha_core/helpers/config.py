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
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from alpha_core.core.enums import AssetClass, Venue


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


class RetryConfig(BaseModel):
    """Backoff policy for transient broker errors (lifted from Vega for the ccxt adapter)."""

    model_config = ConfigDict(extra="forbid")
    max_attempts: int = 5
    base_backoff: float = 0.5
    max_backoff: float = 30.0
    jitter: bool = True


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


# --- Statistical-rigor config (rigor.yaml) — CPCV + PBO (B1a.4) ---------------------
class CPCVConfig(BaseModel):
    """Combinatorial Purged Cross-Validation parameters (López de Prado, AFML ch. 12)."""

    model_config = ConfigDict(extra="forbid")
    n_groups: int = Field(gt=1)  # N: contiguous groups the in-sample series is split into
    n_test_groups: int = Field(gt=0)  # k: groups held out per combinatorial test fold
    embargo_frac: float = Field(ge=0, le=1)  # fraction purged/embargoed around each test block

    @model_validator(mode="after")
    def _check_test_groups(self) -> Self:
        if self.n_test_groups >= self.n_groups:
            raise ValueError(
                f"n_test_groups={self.n_test_groups} must be < n_groups={self.n_groups} "
                "(at least one group must remain for training)"
            )
        return self


class PBOConfig(BaseModel):
    """Probability-of-Backtest-Overfitting parameters (Bailey et al. 2017, CSCV)."""

    model_config = ConfigDict(extra="forbid")
    n_splits: int = Field(gt=1)  # S: row-subsets of the performance matrix (must be even)
    threshold: float = Field(ge=0, le=1)  # PBO above this flags selection overfitting

    @model_validator(mode="after")
    def _check_even_splits(self) -> Self:
        if self.n_splits % 2 != 0:
            raise ValueError(f"n_splits={self.n_splits} must be even (CSCV uses S/2 in-sample)")
        return self


class DSRConfig(BaseModel):
    """Deflated Sharpe Ratio threshold (Bailey & López de Prado 2014, B1a.5)."""

    model_config = ConfigDict(extra="forbid")
    threshold: float = Field(gt=0, lt=1)  # minimum DSR (probability) to be significant


class HoldoutConfig(BaseModel):
    """Roll-forward locked holdout window (R5/R6, B1a.6)."""

    model_config = ConfigDict(extra="forbid")
    fraction: float = Field(gt=0, lt=1)  # most-recent fraction of the span locked away


class PrescreenConfig(BaseModel):
    """vectorbt coarse pre-screen upstream of CPCV (R7, B1a.8)."""

    model_config = ConfigDict(extra="forbid")
    min_sharpe: float  # cull below this median coarse OOS per-bar Sharpe
    n_windows: int = Field(gt=0)  # coarse out-of-sample walk-forward windows


class CpcvBudgetConfig(BaseModel):
    """CPCV compute budget + parallel pool (R7, B1a.8)."""

    model_config = ConfigDict(extra="forbid")
    budget_seconds: float = Field(gt=0)  # a night's wall-clock compute budget
    pool_size: int = Field(gt=0)  # parallel CPCV workers


class CalibrationConfig(BaseModel):
    """Population-calibration error-rate gate (R14, B1a.9) — the [You]-ratified Tier-2 params."""

    model_config = ConfigDict(extra="forbid")
    false_promote_max: float = Field(gt=0, lt=1)  # max rate of promoting noise/overfit
    false_reject_max: float = Field(gt=0, lt=1)  # max rate of rejecting a real edge
    n_candidates: int = Field(gt=1)  # candidates per synthetic population
    n_obs: int = Field(gt=1)  # return-series length per candidate
    edge_drift: float = Field(gt=0)  # the planted edge's per-bar mean
    oos_fraction: float = Field(gt=0, lt=1)  # the OOS slice the gate judges on


class QuantAnalystConfig(BaseModel):
    """Quant-analyst verdict thresholds (B1b.2) — the promote/reject/revise call."""

    model_config = ConfigDict(extra="forbid")
    min_oos_sharpe: float  # coarse OOS-edge screen: reject a candidate whose OOS Sharpe is <= this
    fold_consistency_min: float = Field(ge=0, le=1)  # min positive-fold fraction to promote
    oos_fraction: float = Field(gt=0, lt=1)  # the out-of-sample slice the verdict judges


class RigorConfig(BaseModel):
    """``rigor.yaml`` — the statistical-rigor tunables (CPCV embargo + PBO + DSR + holdout +
    the vectorbt pre-screen + the CPCV compute budget + population calibration + the
    quant-analyst verdict)."""

    model_config = ConfigDict(extra="forbid")
    cpcv: CPCVConfig
    pbo: PBOConfig
    dsr: DSRConfig
    holdout: HoldoutConfig
    prescreen: PrescreenConfig
    cpcv_budget: CpcvBudgetConfig
    calibration: CalibrationConfig
    quant_analyst: QuantAnalystConfig


def load_rigor_config() -> RigorConfig:
    """Load + validate ``config/rigor.yaml`` (honors ``ALPHA_CONFIG_DIR``)."""
    return RigorConfig.model_validate(load_yaml("rigor.yaml"))


# --- Discovery universe (discovery.yaml) — the cell -> cold-store series map (B1b.3d) ----
class DiscoveryCellConfig(BaseModel):
    """One discovery cell: the ``(market, window)`` the loop searches mapped to the cold-store
    series it backtests on. The bars are **in-sample only** — the cold store holds no holdout
    (``seal_dataset`` routes it to the separate gate-only store), so this never names a holdout
    boundary (TEST-3/R6)."""

    model_config = ConfigDict(extra="forbid")
    market: AssetClass  # the cell's asset class (matches the series' bars)
    window: str = Field(min_length=1, max_length=64)  # the cell label (also the ledger cell key)
    symbol: str = Field(min_length=1)  # the cold-store series symbol (e.g. "BTCUSDT")
    venue: Venue  # the series venue
    interval_seconds: int = Field(gt=0)  # the bar interval
    # discovery-run params (B1b.4): the backtest capital at this cell's market scale (crypto $ vs
    # equity ₹) — the per-cell risk base_capital is aligned to it so limits scale with the cell;
    # and which vetted templates to search here (omit / null = all registered templates).
    starting_cash: Decimal = Field(default=Decimal("1000000"), gt=0)
    templates: list[str] | None = None

    @field_validator("window")
    @classmethod
    def _no_key_separator(cls, value: str) -> str:
        # the window doubles as the proposal-ledger cell key, where "|" is the field separator.
        if "|" in value:
            raise ValueError("window must not contain the '|' cell-key separator")
        return value

    @field_validator("templates")
    @classmethod
    def _templates_omitted_or_non_empty(cls, value: list[str] | None) -> list[str] | None:
        # omit (null) = all registered templates; an explicit [] is ambiguous (means "none"?) and
        # almost always a mistake — reject it so the intent is never silently widened to "all".
        if value is not None and not value:
            raise ValueError("templates must be omitted (= all registered) or a non-empty list")
        return value


class DiscoveryConfig(BaseModel):
    """``discovery.yaml`` — the discovery universe: every research cell mapped to its series."""

    model_config = ConfigDict(extra="forbid")
    cells: list[DiscoveryCellConfig] = Field(min_length=1)  # an empty universe is a config error
    n_candidates: int = Field(default=8, gt=1)  # proposals per (cell, template) discovery cycle

    @model_validator(mode="after")
    def _unique_cells(self) -> Self:
        """A ``(market, window)`` cell must resolve to exactly one series — reject duplicates at
        load (else the adapter would silently shadow one mapping with another)."""
        seen: set[tuple[AssetClass, str]] = set()
        for cell in self.cells:
            key = (cell.market, cell.window)
            if key in seen:
                raise ValueError(
                    f"duplicate discovery cell {cell.market.value}/{cell.window!r}: "
                    "each (market, window) must map to exactly one series"
                )
            seen.add(key)
        return self


def load_discovery_config() -> DiscoveryConfig:
    """Load + validate ``config/discovery.yaml`` (honors ``ALPHA_CONFIG_DIR``)."""
    return DiscoveryConfig.model_validate(load_yaml("discovery.yaml"))
