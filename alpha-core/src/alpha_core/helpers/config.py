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


class HoldoutEvalConfig(BaseModel):
    """One-shot holdout-gate evaluation (the M3.0-follow-on power fix).

    The holdout window is 100% out-of-sample **by construction** — the candidate is frozen
    (params + deflation inputs locked from the in-sample sweep) before the gate reads a
    single holdout bar — so the gate judges the WHOLE window (``1.0``), not the
    quant-analyst's in-sample/OOS split. ``le=1`` deliberately admits 1.0, unlike the
    in-sample ``oos_fraction`` fields where a slice of 1 would leave no in-sample."""

    model_config = ConfigDict(extra="forbid")
    oos_fraction: float = Field(default=1.0, gt=0, le=1)  # slice of the holdout the gate judges


class PaperEvalConfig(BaseModel):
    """Paper-run acceptance thresholds (M3.7) — does forward paper confirm the backtest?

    The paper edge must persist (an absolute Sharpe floor) **and** keep a minimum fraction
    of the backtest Sharpe (so slippage / fees / real fills haven't eaten the edge)."""

    model_config = ConfigDict(extra="forbid")
    min_obs: int = Field(
        default=60, gt=0
    )  # min paper return-obs: too few -> a lucky Sharpe -> fail
    min_paper_sharpe: float = 0.5  # absolute floor on the live paper Sharpe
    min_sharpe_retention: float = Field(
        default=0.5, ge=0, le=1
    )  # paper/backtest Sharpe ratio floor


class RigorConfig(BaseModel):
    """``rigor.yaml`` — the statistical-rigor tunables (CPCV embargo + PBO + DSR + holdout +
    the vectorbt pre-screen + the CPCV compute budget + population calibration + the
    quant-analyst verdict + the paper-run acceptance thresholds)."""

    model_config = ConfigDict(extra="forbid")
    cpcv: CPCVConfig
    pbo: PBOConfig
    dsr: DSRConfig
    holdout: HoldoutConfig
    prescreen: PrescreenConfig
    cpcv_budget: CpcvBudgetConfig
    calibration: CalibrationConfig
    quant_analyst: QuantAnalystConfig
    holdout_eval: HoldoutEvalConfig = Field(default_factory=HoldoutEvalConfig)  # optional section
    paper_eval: PaperEvalConfig = Field(default_factory=PaperEvalConfig)  # M3.7 (optional section)


def load_rigor_config() -> RigorConfig:
    """Load + validate ``config/rigor.yaml`` (honors ``ALPHA_CONFIG_DIR``)."""
    return RigorConfig.model_validate(load_yaml("rigor.yaml"))


# --- Discovery universe (discovery.yaml) — the cell -> cold-store series map (B1b.3d) ----
def _check_templates(value: list[str] | None) -> list[str] | None:
    """Shared cell/panel ``templates`` rule: omit (null) = all registered templates; an explicit
    ``[]`` is ambiguous (means "none"?) and almost always a mistake — reject it so the intent is
    never silently widened to "all"."""
    if value is not None and not value:
        raise ValueError("templates must be omitted (= all registered) or a non-empty list")
    return value


def _reject_pipe(value: str, *, label: str) -> str:
    """Shared cell/panel rule: the value doubles as a proposal-ledger cell key, where ``|`` is the
    field separator (``proposal_ledger.cell_key``): reject it at load, not at key-build time."""
    if "|" in value:
        raise ValueError(f"{label} must not contain the '|' cell-key separator")
    return value


class DiscoveryCellConfig(BaseModel):
    """One discovery cell: the ``(market, window)`` the loop searches mapped to the cold-store
    series it backtests on. The bars are **in-sample only** — the cold store holds no holdout
    (``seal_cold_store`` routes it to the separate gate-only store), so this never names a holdout
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
        return _reject_pipe(value, label="window")

    @field_validator("templates")
    @classmethod
    def _templates_omitted_or_non_empty(cls, value: list[str] | None) -> list[str] | None:
        return _check_templates(value)


class DiscoveryPanelConfig(BaseModel):
    """One cross-sectional discovery *panel* (M3.0): a group of symbols sharing a ``(market,
    venue, interval)``, backtested **together** so a strategy can rank the cross-section (relative
    strength — long the top, short the bottom). Where a ``DiscoveryCellConfig`` names one series, a
    panel names the whole universe a cross-sectional template (the panel backtester) ranks each
    rebalance. The bars are **in-sample only**: the cold store holds no holdout (the seal routes it
    to the gate-only store), so a panel never names a holdout boundary either (TEST-3/R6)."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)  # the panel label (also the ledger cell key)
    market: AssetClass  # the panel's asset class (matches every member series' bars)
    venue: Venue  # the shared series venue
    interval_seconds: int = Field(gt=0)  # the shared bar interval
    symbols: list[str] = Field(min_length=2)  # the universe (a cross-section needs >= 2 names)
    # the backtest capital at this panel's market scale (crypto $ vs equity ₹), like a cell; and
    # which vetted cross-sectional templates to search here (omit / null = all registered).
    starting_cash: Decimal = Field(default=Decimal("1000000"), gt=0)
    templates: list[str] | None = None

    @field_validator("name")
    @classmethod
    def _no_key_separator(cls, value: str) -> str:
        return _reject_pipe(value, label="panel name")

    @field_validator("symbols")
    @classmethod
    def _symbols_clean_and_unique(cls, value: list[str]) -> list[str]:
        for symbol in value:
            if not symbol.strip():
                raise ValueError("panel symbols must each be non-empty")
            if symbol != symbol.strip():  # surrounding whitespace -> the same instrument twice
                raise ValueError(f"panel symbol {symbol!r} must not have surrounding whitespace")
        if len(set(value)) != len(value):
            raise ValueError("panel symbols must be unique within a panel")
        return value

    @field_validator("templates")
    @classmethod
    def _templates_omitted_or_non_empty(cls, value: list[str] | None) -> list[str] | None:
        return _check_templates(value)


class DiscoveryBasisConfig(BaseModel):
    """One delta-neutral *basis* cell (M3.0 basis track): a two-leg book — short each selected
    perp, long the same symbol's spot — harvesting the funding a short perp receives with the
    price risk hedged out per name. Where a ``DiscoveryPanelConfig`` names ONE universe, a basis
    cell names TWO (the perp panel and its spot twin, joined by symbol); ``name`` is the ledger
    cell key the basis templates sweep under, and must not collide with any panel name."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)  # the basis cell label (also the ledger key)
    market: AssetClass  # the shared asset class of both legs' panels
    perp_panel: str = Field(min_length=1)  # the funding-paying leg (a `panels` name)
    spot_panel: str = Field(min_length=1)  # the hedge leg (a `panels` name, the spot twin)

    @field_validator("name")
    @classmethod
    def _no_key_separator(cls, value: str) -> str:
        return _reject_pipe(value, label="basis cell name")


class DiscoveryConfig(BaseModel):
    """``discovery.yaml`` — the discovery universe: every research cell mapped to its series, plus
    any cross-sectional ``panels`` (M3.0) that name a multi-symbol universe to rank together, plus
    any two-leg ``basis_panels`` (the M3.0 basis track) joining a perp panel to its spot twin."""

    model_config = ConfigDict(extra="forbid")
    cells: list[DiscoveryCellConfig] = Field(min_length=1)  # an empty universe is a config error
    # the cross-sectional panels (M3.0) — a multi-symbol universe ranked together (default: none).
    panels: list[DiscoveryPanelConfig] = Field(default_factory=list)
    # the delta-neutral basis cells (M3.0 basis track) — perp panel x spot twin (default: none).
    basis_panels: list[DiscoveryBasisConfig] = Field(default_factory=list)
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

    @model_validator(mode="after")
    def _unique_panels(self) -> Self:
        """A panel ``name`` is its ledger cell key — reject duplicate panel names at load."""
        seen: set[str] = set()
        for panel in self.panels:
            if panel.name in seen:
                raise ValueError(
                    f"duplicate discovery panel {panel.name!r}: panel names must be unique"
                )
            seen.add(panel.name)
        return self

    @model_validator(mode="after")
    def _basis_panels_resolve(self) -> Self:
        """A basis cell must (a) have a unique name that collides with NO panel name (both are
        ledger cell keys in the same (market, window) space), and (b) reference two *existing*
        panels of its own market — a dangling or cross-market leg is a config bug, not a runtime
        condition."""
        panels_by_name = {panel.name: panel for panel in self.panels}
        seen: set[str] = set()
        for basis in self.basis_panels:
            if basis.name in seen:
                raise ValueError(f"duplicate basis cell {basis.name!r}: basis names must be unique")
            if basis.name in panels_by_name:
                raise ValueError(
                    f"basis cell {basis.name!r} collides with a panel name — both are ledger "
                    "cell keys, so they must be distinct"
                )
            seen.add(basis.name)
            for label, ref in (("perp_panel", basis.perp_panel), ("spot_panel", basis.spot_panel)):
                leg = panels_by_name.get(ref)
                if leg is None:
                    raise ValueError(
                        f"basis cell {basis.name!r}: {label} {ref!r} is not a configured panel"
                    )
                if leg.market is not basis.market:
                    raise ValueError(
                        f"basis cell {basis.name!r}: {label} {ref!r} is {leg.market.value}, "
                        f"not {basis.market.value} — both legs must share the cell's market"
                    )
        return self


def load_discovery_config() -> DiscoveryConfig:
    """Load + validate ``config/discovery.yaml`` (honors ``ALPHA_CONFIG_DIR``)."""
    return DiscoveryConfig.model_validate(load_yaml("discovery.yaml"))
