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


def dollar_neutral_book(scores: Mapping[str, Decimal], top_k: int) -> dict[str, Decimal]:
    """Long the top ``top_k`` names by score and short the bottom ``top_k``, equal-weight and
    dollar-neutral (longs sum ``+1``, shorts ``-1``; gross 2, net 0). Returns ``{}`` when fewer than
    ``2 * top_k`` names are scored. Deterministic: score descending, symbol as the tiebreak."""
    if len(scores) < 2 * top_k:
        return {}
    ranked = sorted(scores, key=lambda s: (-scores[s], s))
    leg = Decimal(1) / Decimal(top_k)  # equal-weight within each leg
    weights = dict.fromkeys(ranked[:top_k], leg)
    weights.update(dict.fromkeys(ranked[-top_k:], -leg))
    return weights


def _trailing_mean(values: Sequence[Decimal], period: int) -> Decimal | None:
    """Mean of the last ``period`` values, or ``None`` if the series is too short."""
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window, Decimal(0)) / Decimal(period)


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
        return dollar_neutral_book(scores, self._cfg.top_k)


class CrossSectionalMomentum(_CrossSectional):
    """Long the strongest trailing-return names, short the weakest (cross-sectional momentum)."""

    _SIGN = 1


class CrossSectionalReversal(_CrossSectional):
    """Long the weakest trailing-return names, short the strongest (cross-sectional reversal)."""

    _SIGN = -1


def _bar_returns(closes: Sequence[Decimal]) -> list[Decimal]:
    """Consecutive bar-to-bar returns over ``closes`` (skipping non-positive bases, mirroring the
    base<=0 score guard)."""
    return [closes[j + 1] / closes[j] - Decimal(1) for j in range(len(closes) - 1) if closes[j] > 0]


def _trailing_vol(closes: Sequence[Decimal], period: int) -> Decimal | None:
    """Population stdev of the last ``period`` bar returns, or ``None`` if the series is too
    short (needs ``period + 1`` closes) or a base price was non-positive."""
    if period <= 0 or len(closes) < period + 1:
        return None
    returns = _bar_returns(closes[-(period + 1) :])
    if len(returns) < 2:
        return None
    mean = sum(returns, Decimal(0)) / Decimal(len(returns))
    variance = sum(((r - mean) ** 2 for r in returns), Decimal(0)) / Decimal(len(returns))
    return variance.sqrt()


def vol_scaled_book(
    base: Mapping[str, Decimal], closes: Mapping[str, Sequence[Decimal]], lookback: int
) -> dict[str, Decimal]:
    """Re-weight a dollar-neutral book's legs inversely to each name's trailing vol (risk parity
    within the leg), renormalizing so longs still sum ``+1`` and shorts ``-1`` — the same names,
    the same dollar-neutrality, but the high-vol names no longer dominate the book's risk. If any
    leg member's vol is unavailable or 0 (a degenerate flat series), that leg falls back to equal
    weight (deterministic, never a division blow-up)."""
    out: dict[str, Decimal] = {}
    for sign in (Decimal(1), Decimal(-1)):
        leg = [s for s, w in base.items() if w * sign > 0]
        if not leg:
            continue
        vols = {s: _trailing_vol(closes[s], lookback) for s in leg}
        if any(v is None or v <= 0 for v in vols.values()):
            inverse = dict.fromkeys(leg, Decimal(1))  # fall back to equal weight within the leg
        else:
            inverse = {s: Decimal(1) / v for s, v in vols.items() if v is not None}
        total = sum(inverse.values(), Decimal(0))
        out.update({s: sign * inverse[s] / total for s in leg})
    return out


class _VolScaled(_CrossSectional):
    """The base rank + dollar-neutral selection, with inverse-vol weights within each leg."""

    def target_weights(self, closes: Mapping[str, Sequence[Decimal]]) -> dict[str, Decimal]:
        return vol_scaled_book(super().target_weights(closes), closes, self._cfg.lookback)


class VolScaledMomentum(_VolScaled):
    """Cross-sectional momentum with inverse-trailing-vol leg weights (risk parity in the leg)."""

    _SIGN = 1


class VolScaledReversal(_VolScaled):
    """Cross-sectional reversal with inverse-trailing-vol leg weights (risk parity in the leg)."""

    _SIGN = -1


class BetaNeutralConfig(CrossSectionalConfig):
    """A cross-sectional config with the benchmark the book hedges its beta against. The hedge
    symbol is config (per-strategy yaml / template default), never a magic string in the fold."""

    hedge_symbol: str = "BTCUSDT"  # the panel member the book's beta is hedged with


def trailing_beta(
    closes: Sequence[Decimal], benchmark: Sequence[Decimal], period: int
) -> Decimal | None:
    """OLS beta of the last ``period`` bar returns of ``closes`` on ``benchmark``'s, or ``None``
    if either series is too short. A flat benchmark (zero variance) yields beta 0 — there is no
    benchmark risk to hedge."""
    r_s = _trailing_returns_window(closes, period)
    r_b = _trailing_returns_window(benchmark, period)
    if r_s is None or r_b is None or len(r_s) != len(r_b):
        return None
    n = Decimal(len(r_b))
    mean_s = sum(r_s, Decimal(0)) / n
    mean_b = sum(r_b, Decimal(0)) / n
    var_b = sum(((b - mean_b) ** 2 for b in r_b), Decimal(0)) / n
    if var_b == 0:
        return Decimal(0)
    cov = sum(((s - mean_s) * (b - mean_b) for s, b in zip(r_s, r_b, strict=True)), Decimal(0)) / n
    return cov / var_b


def _trailing_returns_window(closes: Sequence[Decimal], period: int) -> list[Decimal] | None:
    """The last ``period`` bar returns (needs ``period + 1`` closes, all bases positive)."""
    if period <= 0 or len(closes) < period + 1:
        return None
    window = closes[-(period + 1) :]
    returns = _bar_returns(window)
    return returns if len(returns) == period else None


class BetaNeutralMomentum(_CrossSectional):
    """Cross-sectional momentum with the book's benchmark beta hedged out.

    The base dollar-neutral book is net-zero in DOLLARS but usually not in BETA (the winners leg
    often carries more market beta than the losers leg), so a "market-neutral" momentum book still
    bleeds with BTC. This variant estimates each held name's trailing beta to the configured
    ``hedge_symbol`` and adds a hedge position ``-(Σ w·β)`` in it, driving the book's ex-ante
    benchmark beta to ~0. No hedge data (the benchmark not scoreable that bar, or any held name's
    beta unavailable) -> no book — a beta-neutral book without its hedge is not this strategy."""

    _SIGN = 1

    def __init__(self, config: BetaNeutralConfig) -> None:
        super().__init__(config)
        self._hedge = config.hedge_symbol

    def target_weights(self, closes: Mapping[str, Sequence[Decimal]]) -> dict[str, Decimal]:
        if self._hedge not in closes:
            return {}
        base = super().target_weights(closes)
        if not base:
            return {}
        benchmark = closes[self._hedge]
        betas: dict[str, Decimal] = {}
        for symbol in base:
            beta = trailing_beta(closes[symbol], benchmark, self._cfg.lookback)
            if beta is None:
                return {}  # a held name without a beta estimate -> the hedge would be wrong
            betas[symbol] = beta
        hedge = -sum((base[s] * betas[s] for s in base), Decimal(0))
        out = dict(base)
        out[self._hedge] = out.get(self._hedge, Decimal(0)) + hedge
        return out


class FundingCarry:
    """Cross-sectional funding **carry** (M3.0): rank the panel by trailing-mean funding and go
    dollar-neutral — **short** the highest-funding perps (a short receives funding) and **long** the
    lowest / most-negative (a long is paid to hold them). The harvested spread is a *cash flow*, a
    different return source than price direction.

    Structurally a ``PanelStrategy`` (config + ``target_weights``), so the panel machinery drives
    it, but the funding backtester feeds it the funding cross-section (not closes) and adds the
    carry P&L to the price P&L. Look-ahead-clean: the caller passes funding up to the current bar.
    """

    def __init__(self, config: CrossSectionalConfig) -> None:
        self._cfg = config

    @property
    def config(self) -> CrossSectionalConfig:
        return self._cfg

    def target_weights(self, funding: Mapping[str, Sequence[Decimal]]) -> dict[str, Decimal]:
        """Dollar-neutral weights from the cross-section of trailing-mean funding (each sequence is
        a symbol's per-bar funding up to the current bar). Carry scores by -(trailing funding): the
        lowest-funding names rank highest (long), the highest-funding lowest (short)."""
        scores = {
            symbol: -mean
            for symbol, series in funding.items()
            if (mean := _trailing_mean(series, self._cfg.lookback)) is not None
        }
        return dollar_neutral_book(scores, self._cfg.top_k)


class BasisCarry:
    """Delta-neutral basis carry (the M3.0 basis track): select the highest trailing-mean-funding
    perps and hold each as ONE UNIT of basis — **short the perp, long the same symbol's spot**,
    equal notional — so the funding a short perp receives is harvested with the price risk hedged
    out *per name* (a weight here means "units of the two-leg book", never a directional position;
    the basis backtester trades both legs).

    Selection, not long/short ranking: only names whose trailing-mean funding is **positive** are
    candidates (a negative-funding basis position *pays* funding — a real carry desk goes flat
    rather than bleed), the top ``top_k`` by mean are held, equal-weighted over however many
    qualify (fewer than ``top_k`` positive names -> a smaller book of just those; none -> ``{}``,
    the flat book). Look-ahead-clean: the caller passes funding up to the current bar.
    Deterministic: mean descending, symbol as the tiebreak.
    """

    def __init__(self, config: CrossSectionalConfig) -> None:
        self._cfg = config

    @property
    def config(self) -> CrossSectionalConfig:
        return self._cfg

    def target_weights(self, funding: Mapping[str, Sequence[Decimal]]) -> dict[str, Decimal]:
        """Equal weights over the top-``top_k`` positive trailing-mean-funding names (each
        sequence is a symbol's per-bar funding up to the current bar), or ``{}`` when none
        qualify — the flat book of a carry desk with nothing worth harvesting."""
        scores = {
            symbol: mean
            for symbol, series in funding.items()
            if (mean := _trailing_mean(series, self._cfg.lookback)) is not None and mean > 0
        }
        if not scores:
            return {}
        ranked = sorted(scores, key=lambda s: (-scores[s], s))[: self._cfg.top_k]
        weight = Decimal(1) / Decimal(len(ranked))
        return dict.fromkeys(ranked, weight)


_DAYS_PER_YEAR = Decimal(365)  # crypto funds every calendar day — the annual<->daily rate bridge


class BasisHoldConfig(CrossSectionalConfig):
    """The low-churn basis book's parameters (the M3.0 basis follow-on): **hysteresis** thresholds
    around the trailing-mean funding, on top of the shared lookback/top_k. ``holding_period`` is
    the membership-CHECK cadence (weekly by default — between checks the book is untouched);
    ``top_k`` caps breadth. Both default rather than search — the searched space stays tiny
    (lookback x entry rate), the pre-registered discipline of a structural signal."""

    top_k: int = Field(default=8, gt=0)  # book-breadth cap (fixed, not searched)
    holding_period: int = Field(default=7, gt=0)  # weekly membership checks (fixed, not searched)
    entry_rate_annual: Decimal = Field(gt=0)  # a name ENTERS at trailing funding >= this (ann.)
    exit_fraction: Decimal = Field(default=Decimal("0.5"), gt=0, le=1)  # exit = entry x this


class BasisCarryHold:
    """Low-churn **hysteresis** basis carry — how a real carry desk runs the book (and the exact
    fix for what killed ``BasisCarry``: post-2022 carry was ~breakeven net of re-ranking churn).

    Membership, checked every ``holding_period`` bars, is stateful in the *held book* the fold
    passes back in (the strategy object itself stays pure):

    - a name **enters** only when its trailing-mean funding >= ``entry_rate_annual`` (per-day
      equivalent);
    - a **held** name is RETAINED while its trailing mean >= ``entry x exit_fraction`` — the
      hysteresis band where a re-ranking book would churn;
    - held names keep their slots (never swapped out for a marginally-hotter entrant); free
      slots go to the highest-funding new qualifiers; equal weight over the final book; ``{}``
      (flat) when nothing qualifies.

    Look-ahead-clean: the caller passes funding up to the current bar. Deterministic: mean
    descending, symbol tiebreak.
    """

    def __init__(self, config: BasisHoldConfig) -> None:
        self._cfg = config

    @property
    def config(self) -> BasisHoldConfig:
        return self._cfg

    def rebalance(
        self, funding: Mapping[str, Sequence[Decimal]], held: Mapping[str, Decimal]
    ) -> dict[str, Decimal]:
        """The next book given the funding cross-section AND the currently-held book (each
        funding sequence is the symbol's per-bar funding up to the current bar)."""
        entry = self._cfg.entry_rate_annual / _DAYS_PER_YEAR
        exit_threshold = entry * self._cfg.exit_fraction
        means = {
            symbol: mean
            for symbol, series in funding.items()
            if (mean := _trailing_mean(series, self._cfg.lookback)) is not None
        }
        # held names retained through the hysteresis band; one absent from the cross-section
        # (ineligible this bar — a leg gone) cannot be verified and is dropped.
        kept = sorted(s for s in held if s in means and means[s] >= exit_threshold)
        slots = self._cfg.top_k - len(kept)
        entrants = sorted(
            (s for s, mean in means.items() if s not in held and mean >= entry),
            key=lambda s: (-means[s], s),
        )[: max(slots, 0)]
        book = kept + entrants
        if not book:
            return {}
        weight = Decimal(1) / Decimal(len(book))
        return dict.fromkeys(book, weight)
