"""The constrained strategist (B1b.1) — propose a valid, original strategy config by
parameterizing a vetted template within a bounded ("whitelisted") param space.

The strategist is the first stage of AI discovery (Phase 1b). It is deliberately *constrained*:
it does not write code, it picks parameter values for the R7 seed templates (the registry), each
within a vetted range. Three guarantees fall out of the design:

* **Holdout isolation (TEST-3) — structural.** The strategist takes *no market data at all*; it
  proposes configs only. It therefore has no path to the holdout (or to any bars) — the isolation
  is enforced by the absence of a data input, not by a runtime check. Evaluating a proposal on
  in-sample data is the quant-analyst's job (B1b.2).
* **Originality + trial accounting (R4).** Every accepted proposal is fingerprinted and recorded in
  the ``ProposalLedger``, which is *idempotent*: a fingerprint already tried in the cell is a no-op,
  so the strategist never re-proposes one **and** the per-cell trial count the Deflated Sharpe Ratio
  deflates by can never be inflated by a re-proposal. Originality holds across runs (the ledger is
  durable) — there is no in-memory ``seen`` set to hydrate, and no separate counter to drift.

The *proposer* — how parameter values are chosen — is injectable. The default ``RandomProposer``
samples the bounded space (seeded → reproducible); the economic-rationale **LLM** proposer is the
pod-agent wrapper (Mac-CLI + Lemma), dropped in later behind the same ``Proposer`` protocol.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from alpha_core.core.enums import AssetClass
from alpha_core.core.interfaces import Strategy
from alpha_core.research.proposal_ledger import ProposalLedger, cell_key
from alpha_core.strategy.examples.bollinger_squeeze import BollingerSqueeze, BollingerSqueezeConfig
from alpha_core.strategy.examples.donchian_atr import DonchianAtr, DonchianAtrConfig
from alpha_core.strategy.examples.ma_crossover import MaCrossover, MaCrossoverConfig
from alpha_core.strategy.examples.macd import Macd, MacdConfig
from alpha_core.strategy.examples.momentum_roc import MomentumRoc, MomentumRocConfig
from alpha_core.strategy.examples.opening_range_breakout import (
    OpeningRangeBreakout,
    OpeningRangeBreakoutConfig,
)
from alpha_core.strategy.examples.rsi_bollinger import RsiBollinger, RsiBollingerConfig
from alpha_core.strategy.examples.trend_pullback import TrendPullback, TrendPullbackConfig
from alpha_core.strategy.examples.vwap_reversion import VwapReversion, VwapReversionConfig

ParamValue = int | Decimal


class StrategistError(RuntimeError):
    """The strategist could not produce a valid, original proposal — a *deterministic* cause:
    an unknown template or an invalid cell (a caller bug, fix the inputs)."""


class CellSaturated(StrategistError):
    """The cell's bounded space is exhausted — every proposal the proposer makes is already
    recorded. A *resource* condition (not a caller bug): the loop stops proposing here."""


# --- the bounded param space (the vetted, whitelisted ranges the strategist may explore) -------


@dataclass(frozen=True, slots=True)
class IntRange:
    """An inclusive integer range ``[low, high]``."""

    low: int
    high: int

    def __post_init__(self) -> None:
        if self.low > self.high:
            raise ValueError(f"IntRange low {self.low} > high {self.high}")

    def sample(self, rng: random.Random) -> int:
        return rng.randint(self.low, self.high)


@dataclass(frozen=True, slots=True)
class DecimalRange:
    """A ``Decimal`` grid ``low, low+step, ..., high`` (``step`` must divide ``high-low``)."""

    low: Decimal
    high: Decimal
    step: Decimal

    def __post_init__(self) -> None:
        if self.step <= 0 or self.low > self.high:
            raise ValueError(f"bad DecimalRange [{self.low}, {self.high}] step {self.step}")
        if (self.high - self.low) % self.step != 0:
            raise ValueError(f"step {self.step} must divide {self.high - self.low}")

    def sample(self, rng: random.Random) -> Decimal:
        steps = int((self.high - self.low) / self.step)
        return self.low + self.step * rng.randint(0, steps)


ParamSpec = IntRange | DecimalRange
ParamSpace = Mapping[str, ParamSpec]


@dataclass(frozen=True, slots=True)
class StrategyTemplate:
    """A vetted template + its bounded edge-parameter space. ``build`` validates a proposal by
    *constructing the strategy* — the config model + the strategy ``__init__`` together reject any
    invalid combination (e.g. ``fast_period >= slow_period``)."""

    name: str
    family: str
    config_cls: type[BaseModel]
    strategy_cls: type[Strategy]
    param_space: ParamSpace

    def build(self, params: Mapping[str, ParamValue]) -> Strategy:
        config = self.config_cls.model_validate(dict(params))
        builder: Any = self.strategy_cls
        strategy: Strategy = builder(config)
        return strategy


# The R7 seed templates (tunable EDGE params only — position size/`quantity` is the risk overlay's
# job, not an edge knob, so the strategist never tunes it).
TEMPLATES: dict[str, StrategyTemplate] = {
    "ma_crossover": StrategyTemplate(
        "ma_crossover",
        "ma_crossover",
        MaCrossoverConfig,
        MaCrossover,
        {"fast_period": IntRange(3, 30), "slow_period": IntRange(10, 100)},
    ),
    "rsi_bollinger": StrategyTemplate(
        "rsi_bollinger",
        "rsi_bollinger",
        RsiBollingerConfig,
        RsiBollinger,
        {
            "rsi_period": IntRange(5, 30),
            "oversold": DecimalRange(Decimal("10"), Decimal("40"), Decimal("5")),
            "overbought": DecimalRange(Decimal("60"), Decimal("90"), Decimal("5")),
            "bollinger_period": IntRange(10, 40),
            "num_std": DecimalRange(Decimal("1"), Decimal("3"), Decimal("0.5")),
        },
    ),
    "momentum_roc": StrategyTemplate(
        "momentum_roc",
        "momentum_roc",
        MomentumRocConfig,
        MomentumRoc,
        {
            "period": IntRange(3, 40),
            "threshold_pct": DecimalRange(Decimal("0.5"), Decimal("5"), Decimal("0.5")),
        },
    ),
    "vwap_reversion": StrategyTemplate(
        "vwap_reversion",
        "vwap_reversion",
        VwapReversionConfig,
        VwapReversion,
        {"band_bps": DecimalRange(Decimal("10"), Decimal("100"), Decimal("5"))},
    ),
    "opening_range_breakout": StrategyTemplate(
        "opening_range_breakout",
        "opening_range_breakout",
        OpeningRangeBreakoutConfig,
        OpeningRangeBreakout,
        {
            "opening_range_minutes": IntRange(5, 60),
            "breakout_buffer_bps": DecimalRange(Decimal("2"), Decimal("30"), Decimal("2")),
        },
    ),
    # Richer templates (M3.0): EMA momentum, volatility-regime breakouts, multi-signal confluence.
    "macd": StrategyTemplate(
        "macd",
        "macd",
        MacdConfig,
        Macd,
        {
            "fast_period": IntRange(5, 20),
            "slow_period": IntRange(21, 50),
            "signal_period": IntRange(5, 15),
        },
    ),
    "donchian_atr": StrategyTemplate(
        "donchian_atr",
        "donchian_atr",
        DonchianAtrConfig,
        DonchianAtr,
        {
            "channel_period": IntRange(10, 50),
            "atr_period": IntRange(7, 21),
            "atr_floor_bps": DecimalRange(Decimal("10"), Decimal("100"), Decimal("10")),
        },
    ),
    "bollinger_squeeze": StrategyTemplate(
        "bollinger_squeeze",
        "bollinger_squeeze",
        BollingerSqueezeConfig,
        BollingerSqueeze,
        {
            "period": IntRange(10, 40),
            "num_std": DecimalRange(Decimal("1.5"), Decimal("3.0"), Decimal("0.5")),
            "squeeze_bps": DecimalRange(Decimal("50"), Decimal("300"), Decimal("50")),
        },
    ),
    "trend_pullback": StrategyTemplate(
        "trend_pullback",
        "trend_pullback",
        TrendPullbackConfig,
        TrendPullback,
        {
            "trend_period": IntRange(20, 100),
            "rsi_period": IntRange(7, 21),
            "pullback_level": DecimalRange(Decimal("20"), Decimal("45"), Decimal("5")),
        },
    ),
}


# --- the proposer (injectable: a seeded sampler here; the LLM proposer is the pod wrapper) ------


class Proposer(Protocol):
    """Chooses parameter values for a template's bounded space.

    Contract: return a dict over the space's parameter names whose values match each spec's type
    (``int`` for ``IntRange``, ``Decimal`` for ``DecimalRange``). The strategist's validate-by-build
    catches *domain*-invalid combinations (it resamples them), not *type/contract* violations — a
    proposer returning the wrong type is a bug, not a resample case.
    """

    def propose(self, space: ParamSpace) -> dict[str, ParamValue]: ...


class RandomProposer:
    """Uniformly samples each parameter within its bounded spec (seeded → reproducible)."""

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    def propose(self, space: ParamSpace) -> dict[str, ParamValue]:
        return {name: spec.sample(self._rng) for name, spec in space.items()}


# --- the proposal + the strategist -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StrategyProposal:
    """A valid, original strategy config the strategist proposes for a cell."""

    template: str
    params: Mapping[str, ParamValue]
    market: AssetClass
    window: str
    trial_index: int  # the ledger's cumulative trial count for the cell after this proposal
    fingerprint: str  # stable hash of (template, params) — the originality key


def _canonical(value: ParamValue) -> str:
    """Representation-independent rendering of a param value: a ``Decimal`` via its *normalized*
    form so ``Decimal('2')``, ``Decimal('2.0')`` and ``Decimal('2.00')`` all render ``'2'`` —
    the RandomProposer (which emits ``'2.0'``) and a future LLM proposer (which might emit ``'2'``)
    can never disagree about the same logical value, so the fingerprint can't leak originality or
    double-count the ledger. ``int`` via ``str``."""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")  # 'f' expands the exponent: 2E+2 -> '200'
    return str(value)


def proposal_fingerprint(template: str, params: Mapping[str, ParamValue]) -> str:
    """A stable, order-independent fingerprint of a (template, params) proposal (see
    ``_canonical`` for why ``Decimal`` values are normalized first)."""
    payload = {"t": template, "p": {k: _canonical(params[k]) for k in sorted(params)}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


class Strategist:
    """Propose valid, original strategy configs by parameterizing vetted templates (B1b.1).

    Takes only the proposal ledger + a proposer — *never* any market data, so it cannot reach the
    holdout (TEST-3 is structural). Each accepted proposal is recorded in the (idempotent, durable)
    ledger, which both enforces originality across runs and is the trial count the DSR deflates by.
    """

    def __init__(
        self, ledger: ProposalLedger, *, proposer: Proposer | None = None, max_attempts: int = 50
    ) -> None:
        self._ledger = ledger
        self._proposer = proposer if proposer is not None else RandomProposer()
        self._max_attempts = max_attempts

    def propose(self, template_name: str, *, market: AssetClass, window: str) -> StrategyProposal:
        """Return a valid, original proposal for ``template_name`` in the ``(market, window)`` cell,
        recording it in the proposal ledger (which both enforces originality across runs and is the
        trial count the DSR deflates by). Raises ``StrategistError`` for an unknown template, an
        invalid cell (e.g. a ``window`` the ledger rejects), or if no original valid proposal is
        found within ``max_attempts`` (the cell is saturated)."""
        template = TEMPLATES.get(template_name)
        if template is None:
            raise StrategistError(f"unknown template {template_name!r}; known: {sorted(TEMPLATES)}")
        try:  # fail fast (and in-contract) on a bad cell, before touching the proposer
            cell_key(market, template.family, window)
        except ValueError as exc:
            raise StrategistError(f"invalid cell for {template_name!r}: {exc}") from exc
        for _ in range(self._max_attempts):
            params = self._proposer.propose(template.param_space)
            try:
                template.build(params)  # validate by constructing (rejects invalid combinations)
            except (ValidationError, ValueError):
                continue
            fingerprint = proposal_fingerprint(template_name, params)
            # record is idempotent: a fingerprint already tried in this cell is a no-op (is_new
            # False, count unchanged), so re-proposals never inflate the DSR's trial count.
            result = self._ledger.record(market, template.family, window, fingerprint)
            if not result.is_new:
                continue  # already tried in this cell — keep proposing for an original one
            return StrategyProposal(
                template_name, params, market, window, result.count, fingerprint
            )
        raise CellSaturated(
            f"no original valid proposal for {template_name!r} within {self._max_attempts} "
            "attempts (the cell is saturated)"
        )
