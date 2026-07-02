"""Index option premium structures (M5.5b) — the first options strategy family.

An **iron condor** sells an OTM call + an OTM put (harvesting the premium retail flow
overpays for) and buys a further-OTM call + put (the wings: defined risk — the honest
price of the tail). The M5.5b family is deliberately structural: pick the expiry nearest
a target DTE, place the short strikes a fixed *percentage of the forward* away, the
wings a fixed percentage further, hold to expiry, one structure at a time. The whole
edge question is whether harvested premium clears the wings + the Indian cost stack —
not parameter luck (a TINY pre-registered grid; see ``OPTIONS_TEMPLATES``).

The class here is PURE selection: given one day's chain (per-expiry quotes), the
parity-implied forwards, and the liquidity floor, return the four legs or ``None``. The
fold (``research/options_backtester.py``) owns time, positions, marking, and costs.
Strike targeting uses the **parity forward** (the strike where |call settle - put
settle| is minimal), so both bhavcopy eras work identically with no external
underlying series and no look-ahead (same-day EOD settles; entries are never on an
expiry day by the DTE floor).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from alpha_core.core.enums import OptionRight
from alpha_core.data.options_store import OptionQuote


class IronCondorConfig(BaseModel):
    """The bounded edge parameters of the EOD iron condor (the searched grid is TINY —
    see ``OPTIONS_TEMPLATES``); the DTE admission window is fixed, not searched."""

    model_config = ConfigDict(extra="forbid")
    target_dte: int = Field(gt=0)  # aim for the expiry nearest this many days out
    short_distance_pct: Decimal = Field(gt=0, lt=1)  # short strikes at forward*(1 +/- this)
    wing_width_pct: Decimal = Field(gt=0, lt=1)  # wings this much further out than the shorts
    min_dte: int = Field(default=3, gt=0)  # never enter closer than this to expiry
    max_dte: int = Field(default=45, gt=0)  # never enter further than this from expiry


@dataclass(frozen=True, slots=True)
class CondorLeg:
    """One leg of the structure: ``quantity`` +1 long / -1 short (per unit of index)."""

    expiry: datetime
    strike: Decimal
    right: OptionRight
    quantity: int  # +1 buy (wing), -1 sell (short strike)


def parity_forward(chain: list[OptionQuote]) -> Decimal | None:
    """The parity-implied forward for ONE expiry's chain: the strike where |C - P| (settle)
    is smallest (put-call parity: C - P = S - K·df, so C ≈ P at K ≈ the forward). Uses only
    strikes quoting BOTH rights with positive settles; ``None`` when no such pair exists.
    Deterministic: smallest gap, lower strike on ties."""
    by_strike: dict[Decimal, dict[OptionRight, Decimal]] = {}
    for q in chain:
        if q.settle > 0:
            by_strike.setdefault(q.strike, {})[q.right] = q.settle
    best: tuple[Decimal, Decimal] | None = None  # (gap, strike)
    for strike, sides in by_strike.items():
        call, put = sides.get(OptionRight.CALL), sides.get(OptionRight.PUT)
        if call is None or put is None:
            continue
        gap = abs(call - put)
        if best is None or (gap, strike) < best:
            best = (gap, strike)
    return best[1] if best is not None else None


def _nearest_liquid_strike(
    chain: list[OptionQuote], right: OptionRight, target: Decimal, min_oi: int
) -> Decimal | None:
    """The listed strike of ``right`` nearest ``target`` with ``open_interest >= min_oi``
    (the liquidity floor). Deterministic: nearest, lower strike on ties. ``None`` if no
    strike clears the floor."""
    candidates = sorted({q.strike for q in chain if q.right is right and q.open_interest >= min_oi})
    if not candidates:
        return None
    return min(candidates, key=lambda k: (abs(k - target), k))


class IronCondorEod:
    """Pure EOD iron-condor selection (the fold owns time/positions/marking/costs)."""

    def __init__(self, config: IronCondorConfig) -> None:
        self._cfg = config

    @property
    def config(self) -> IronCondorConfig:
        return self._cfg

    def pick_expiry(self, dte_by_expiry: dict[datetime, int]) -> datetime | None:
        """The expiry whose DTE is nearest ``target_dte`` within the fixed admission window
        ``[min_dte, max_dte]`` — never an expiry-day entry. Deterministic: nearest, earlier
        expiry on ties. ``None`` when nothing qualifies."""
        cfg = self._cfg
        eligible = [
            (abs(dte - cfg.target_dte), expiry)
            for expiry, dte in dte_by_expiry.items()
            if cfg.min_dte <= dte <= cfg.max_dte
        ]
        return min(eligible)[1] if eligible else None

    def select_structure(
        self, chain: list[OptionQuote], expiry: datetime, forward: Decimal, min_oi: int
    ) -> list[CondorLeg] | None:
        """The four condor legs on ``expiry``'s chain around the parity ``forward``:
        short call/put at ``forward*(1 +/- short_distance_pct)``, long wings
        ``wing_width_pct`` further out — every strike snapped to the nearest LISTED strike
        clearing the ``min_oi`` floor. ``None`` unless all four legs exist and are strictly
        ordered ``put wing < short put < short call < call wing`` (a degenerate/crossed
        structure is no structure)."""
        cfg = self._cfg
        rows = [q for q in chain if q.expiry == expiry]
        short_call = _nearest_liquid_strike(
            rows, OptionRight.CALL, forward * (1 + cfg.short_distance_pct), min_oi
        )
        short_put = _nearest_liquid_strike(
            rows, OptionRight.PUT, forward * (1 - cfg.short_distance_pct), min_oi
        )
        wing_call = _nearest_liquid_strike(
            rows,
            OptionRight.CALL,
            forward * (1 + cfg.short_distance_pct + cfg.wing_width_pct),
            min_oi,
        )
        wing_put = _nearest_liquid_strike(
            rows,
            OptionRight.PUT,
            forward * (1 - cfg.short_distance_pct - cfg.wing_width_pct),
            min_oi,
        )
        if short_call is None or short_put is None or wing_call is None or wing_put is None:
            return None
        if not (wing_put < short_put < short_call < wing_call):
            return None  # snapped onto each other / crossed: no structure today
        return [
            CondorLeg(expiry, short_call, OptionRight.CALL, -1),
            CondorLeg(expiry, short_put, OptionRight.PUT, -1),
            CondorLeg(expiry, wing_call, OptionRight.CALL, 1),
            CondorLeg(expiry, wing_put, OptionRight.PUT, 1),
        ]
