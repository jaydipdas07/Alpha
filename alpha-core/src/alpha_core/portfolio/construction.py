"""Portfolio construction — scored signals → target weights (R4).

The Portfolio stage of the 5-stage pipeline (scaling design §3, PD/D2/D6): a
**pure** allocator that ranks the bar-close cross-section of scored `Signal`s and
emits desired `TargetExposure`s (signed fractions of capital). Pure ⇒ identical in
backtest and live (D10); the execution layer diffs current→target into orders.

`WeightAllocator` implements the `PortfolioConstructor` contract (R1). It is
config-driven (`portfolio.yaml`, R3) — equal-weight top-K today; inverse-vol /
mean-variance are later opt-in methods behind the same interface. Same-instrument
signals (across strategies) are **netted** into one target per `(venue, symbol)`
with attribution recorded (D5). `diff_targets` turns targets into per-name weight
deltas, applying the no-trade band + turnover cap (the rebalance, churn-bounded).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from alpha_core.core.enums import AssetClass, Side
from alpha_core.core.interfaces import PortfolioConstructor
from alpha_core.core.models import Position, Signal, TargetExposure
from alpha_core.helpers.config import PortfolioConfig

# The venue-qualified symbol (e.g. "NSE:RELIANCE") — the key everything nets/diffs
# on. The core is venue-agnostic, so there is no separate venue here (the symbol
# carries it); the executor resolves symbol→venue.
SymbolKey = str


@dataclass(frozen=True)
class _Netted:
    """One instrument's netted conviction across the cross-section."""

    net: Decimal  # signed sum of side*score; sign = net direction, |net| = strength
    asset_class: AssetClass
    strategy_ids: frozenset[str]


def _net_signals(signals: Sequence[Signal]) -> dict[SymbolKey, _Netted]:
    """Net all signals per (venue, symbol): BUY adds, SELL subtracts its score
    (None score → 0). Perfectly offsetting names net to 0 and are dropped later."""
    acc: dict[SymbolKey, _Netted] = {}
    for sig in signals:
        score = sig.score if sig.score is not None else Decimal(0)
        signed = score if sig.side is Side.BUY else -score
        key = sig.symbol
        prev = acc.get(key)
        if prev is None:
            acc[key] = _Netted(signed, sig.asset_class, frozenset({sig.strategy_id}))
        else:
            acc[key] = _Netted(
                prev.net + signed, prev.asset_class, prev.strategy_ids | {sig.strategy_id}
            )
    return acc


class WeightAllocator(PortfolioConstructor):
    """Equal-weight top-K allocator (the default method, D6)."""

    def __init__(self, config: PortfolioConfig) -> None:
        self._cfg = config

    def construct(
        self,
        signals: Sequence[Signal],
        positions: Sequence[Position],
        capital: Decimal,
    ) -> list[TargetExposure]:
        # `positions` and `capital` are part of the contract but unused for
        # equal-weight: targets are capital-independent weight fractions (D2), and
        # the fragmentation floor is enforced at config load (PortfolioConfig._check_
        # book_is_fundable), so no name can round below it here. The executor diffs
        # current→target and sizes against capital. (inverse-vol / value-weighted
        # methods will use both.)
        del positions, capital
        alloc = self._cfg.allocation
        if alloc.method != "equal_weight":
            raise NotImplementedError(
                f"allocation method {alloc.method!r} needs a volatility source (R4 follow-up)"
            )
        # Net the cross-section, drop offsetting names, rank by conviction strength.
        netted = {k: v for k, v in _net_signals(signals).items() if v.net != 0}
        ranked = sorted(netted.items(), key=lambda kv: (-abs(kv[1].net), kv[0]))
        selected = ranked[: alloc.top_k]
        if not selected:
            return []
        base_weight = min(Decimal(1) / Decimal(len(selected)), alloc.max_weight_per_name)
        targets: list[TargetExposure] = []
        for symbol, info in selected:
            direction = Decimal(1) if info.net > 0 else Decimal(-1)
            targets.append(
                TargetExposure(
                    strategy_id="+".join(sorted(info.strategy_ids)),
                    symbol=symbol,
                    asset_class=info.asset_class,
                    weight=base_weight * direction,
                )
            )
        return _apply_gross_cap(targets, alloc.gross_cap)


def _apply_gross_cap(targets: list[TargetExposure], gross_cap: Decimal) -> list[TargetExposure]:
    """Scale all weights down proportionally if sum(|weight|) exceeds the cap."""
    gross = sum((abs(t.weight) for t in targets), Decimal(0))
    if gross <= gross_cap or gross == 0:
        return targets
    scale = gross_cap / gross
    return [t.model_copy(update={"weight": t.weight * scale}) for t in targets]


def diff_targets(
    targets: Sequence[TargetExposure],
    current_weights: dict[SymbolKey, Decimal],
    config: PortfolioConfig,
) -> dict[SymbolKey, Decimal]:
    """Per-(venue, symbol) weight delta to move current→target (the rebalance).

    Pure and price-free — the executor converts a weight delta to a lot-rounded
    order. Applies the **no-trade band** (ignore deltas below the band, killing
    thrash) then the **turnover cap** (scale all deltas down if the pass would
    churn more than the cap). Names absent from ``targets`` diff toward 0 (exit).
    """
    target_w = {t.symbol: t.weight for t in targets}
    band = config.rebalance.no_trade_band
    deltas: dict[SymbolKey, Decimal] = {}
    for key in set(target_w) | set(current_weights):
        delta = target_w.get(key, Decimal(0)) - current_weights.get(key, Decimal(0))
        if abs(delta) >= band:
            deltas[key] = delta
    turnover = sum((abs(d) for d in deltas.values()), Decimal(0))
    cap = config.rebalance.turnover_cap
    if turnover > cap and turnover > 0:
        scale = cap / turnover
        deltas = {k: d * scale for k, d in deltas.items()}
    return deltas
