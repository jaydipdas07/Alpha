"""Cross-sectional rank strategies (M3.0) — the strategy *class* the single-instrument templates
structurally cannot see.

Where a single-instrument strategy sees one symbol's bars, a cross-sectional strategy ranks a whole
*panel* each rebalance by trailing return and goes **dollar-neutral**: long the strongest names,
short the weakest. **Momentum** longs the winners; **reversal** longs the losers — the only
difference is the sign, so the two share one implementation (``_SIGN``). This is the most
OOS-robust systematic anomaly class (a risk-premium / liquidity-provision harvest, not a fragile
price pattern); it exploits relative strength across the panel, which no single-series ``on_bar``
can express.

Pure + look-ahead-clean: ``target_weights`` scores each name from the closes the caller supplies
*up to and including the current bar* (the panel backtester slices; the strategy never peeks ahead).
Money stays ``Decimal`` end-to-end; the panel backtester reduces to ``float`` only for the
statistics plane (the Sharpe the rigor gate judges), never back onto a money path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class CrossSectionalConfig(BaseModel):
    """The bounded edge parameters of a cross-sectional rank strategy.

    ``top_k`` names are held long and ``top_k`` short each rebalance, so the panel must carry at
    least ``2 * top_k`` names with enough history — enforced by the panel backtester against the
    actual universe (the config alone does not know the panel size)."""

    model_config = ConfigDict(extra="forbid")
    lookback: int = Field(gt=0)  # bars of trailing return used to score each name
    top_k: int = Field(gt=0)  # names long, and names short, each rebalance
    holding_period: int = Field(default=1, gt=0)  # bars to hold a book before re-ranking


@runtime_checkable
class PanelStrategy(Protocol):
    """What the panel backtester needs of a cross-sectional strategy: its config (for the rebalance
    cadence + warmup) and a pure ``target_weights`` from the cross-section of closes-so-far."""

    @property
    def config(self) -> CrossSectionalConfig: ...

    def target_weights(self, closes: Mapping[str, Sequence[Decimal]]) -> dict[str, Decimal]: ...


class _CrossSectional:
    """Shared dollar-neutral long-top / short-bottom rank logic; ``_SIGN`` is the only difference
    between momentum (+1, long the winners) and reversal (-1, long the losers)."""

    _SIGN: int

    def __init__(self, config: CrossSectionalConfig) -> None:
        self._cfg = config

    @property
    def config(self) -> CrossSectionalConfig:
        return self._cfg

    def _trailing_return(self, closes: Sequence[Decimal]) -> Decimal | None:
        """Return over the last ``lookback`` bars (``close[-1] / close[-1-lookback] - 1``), or
        ``None`` if the series is too short or the base price is non-positive."""
        n = self._cfg.lookback
        if len(closes) < n + 1:
            return None
        base = closes[-1 - n]
        if base <= 0:
            return None
        return closes[-1] / base - Decimal(1)

    def target_weights(self, closes: Mapping[str, Sequence[Decimal]]) -> dict[str, Decimal]:
        """Dollar-neutral target weights from the cross-section of trailing returns over the closes
        given (each sequence is the symbol's closes up to and including the current bar — the caller
        never passes the future). Longs sum to ``+1`` and shorts to ``-1`` (gross 2, net 0). Returns
        ``{}`` when fewer than ``2 * top_k`` names have a usable score — no book is taken that bar.

        ``_SIGN`` flips the ranking: momentum (+1) longs the highest trailing return, reversal (-1)
        the lowest.
        """
        scores = {
            symbol: Decimal(self._SIGN) * ret
            for symbol, series in closes.items()
            if (ret := self._trailing_return(series)) is not None
        }
        k = self._cfg.top_k
        if len(scores) < 2 * k:
            return {}
        # rank by score desc (symbol tiebreak); long the top k, short the bottom k.
        ranked = sorted(scores, key=lambda s: (-scores[s], s))
        leg = Decimal(1) / Decimal(k)  # equal-weight within each leg
        weights = dict.fromkeys(ranked[:k], leg)
        weights.update(dict.fromkeys(ranked[-k:], -leg))
        return weights


class CrossSectionalMomentum(_CrossSectional):
    """Long the strongest trailing-return names, short the weakest (cross-sectional momentum)."""

    _SIGN = 1


class CrossSectionalReversal(_CrossSectional):
    """Long the weakest trailing-return names, short the strongest (cross-sectional reversal)."""

    _SIGN = -1
