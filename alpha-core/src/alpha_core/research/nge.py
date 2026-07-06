"""Net gamma exposure (NGE) from our own EOD option chains — the Baltussen-form
conditioning input (the intraday-mandate survey §5's "NGE sign from our own EOD chains").

The hypothesis (Baltussen-van Bekkum-Da, "Hedging demand and market intraday momentum"):
when option dealers are net SHORT gamma, their delta-hedging trades WITH the market into
the close, amplifying intraday momentum; long-gamma days damp it. The tradeable form needs
only the SIGN of a daily net-gamma proxy, computed strictly from data available at that
day's close.

**The proxy (declared, deliberately naive):** for one underlying and trading day, take the
NEAREST expiry strictly AFTER the day (front-month gamma dominates; same-day-expiring
contracts are settling — their marks live in the #150/#153 expiry-settle trap and their
gamma dies at the close), price the parity-implied forward off that chain, solve each
contract's Black-76 IV from its settle within a ``moneyness_band`` of the forward, and sum

    NGE_day = Σ  gamma(contract) x open_interest x (+1 for calls, -1 for puts)

— the classic dealers-long-calls/short-puts GEX convention (SqueezeMetrics). Its known
limitation is the fixed sign assumption (true dealer positioning is unobservable from EOD
OI); it is a *proxy*, and the family's registration says so. Contracts whose IV cannot be
solved (outside the no-arb band — stale/crossed marks) are SKIPPED, never fabricated; a day
with no parity pair or no usable contracts yields NO row (missing day = no signal = no
trade downstream — conservative).

**Look-ahead fence:** :func:`shift_to_next_session` converts the raw (day → NGE) series
into the map the STRATEGY reads — ``sign to APPLY on day D = sign(NGE of the previous
available row strictly before D)`` — so the fold can only ever condition on a completed
prior session's chain. The shift happens HERE, once, not in strategy code.

**TEST-3 / cross-family coupling (declared):** the series must be computed from the
options RESEARCH partition only (``options_research`` — the caller passes that store).
The options HOLDOUT (2024-05-24→, virgin) is never touched; consequently the conditioned
family's fold is bounded at the options research boundary, and a FUTURE holdout read of
the conditioned family would require a sanctioned options-holdout NGE computation — a
deliberate, recorded coupling, deferred until such a read is powered.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from itertools import groupby, pairwise
from zoneinfo import ZoneInfo

from alpha_core.core.enums import OptionRight, Venue
from alpha_core.data.options_store import OptionQuote, OptionsStore
from alpha_core.options.pricing import greeks, implied_vol, time_to_expiry
from alpha_core.strategy.examples.index_premium import parity_forward

_IST = ZoneInfo("Asia/Kolkata")
_NSE_CLOSE = time(15, 30)  # settles print at the cash close — the IV anchor instant


@dataclass(frozen=True, slots=True)
class NgeDay:
    """One day's net-gamma-exposure proxy (float statistics plane — never money)."""

    day: datetime  # the IST trading day as a UTC-midnight label (the store convention)
    nge: float  # signed Σ gamma x OI x (call:+1 / put:-1) over the front-expiry band
    n_contracts: int  # contracts that contributed (IV solvable, inside the band)


def compute_nge_series(
    store: OptionsStore,
    *,
    underlying: str = "NIFTY",
    venue: Venue = Venue.NSE,
    moneyness_band: float = 0.15,
) -> list[NgeDay]:
    """The raw per-day NGE series for ``underlying`` from ``store`` (the RESEARCH partition
    — see the module docstring's TEST-3 note). Deterministic; skips unusable days."""
    quotes = store.read(underlying=underlying, venue=venue)
    out: list[NgeDay] = []
    for day, day_iter in groupby(quotes, key=lambda q: q.trade_date):
        day_quotes = list(day_iter)
        front = _front_expiry(day_quotes, day)
        if front is None:
            continue
        chain = [q for q in day_quotes if q.expiry == front]
        forward = parity_forward(chain)
        if forward is None:
            continue
        # IV anchors at THIS day's cash close (trade_date is the IST day's UTC-midnight
        # label, so .date() is the IST calendar date); expiry settles per the NSE default.
        now = datetime.combine(day.date(), _NSE_CLOSE, tzinfo=_IST)
        t_years = time_to_expiry(now, front.date())
        if t_years <= 0:
            continue
        lo = float(forward) * (1.0 - moneyness_band)
        hi = float(forward) * (1.0 + moneyness_band)
        total = 0.0
        used = 0
        for q in chain:
            if q.open_interest <= 0 or q.settle <= 0:
                continue
            if not lo <= float(q.strike) <= hi:
                continue
            iv = implied_vol(q.settle, forward, q.strike, t_years, q.right)
            if iv is None:
                continue
            gamma = greeks(forward, q.strike, iv, t_years, q.right).gamma
            if gamma is None:
                continue
            side = 1.0 if q.right is OptionRight.CALL else -1.0
            total += float(gamma) * float(q.open_interest) * side
            used += 1
        if used == 0:
            continue
        out.append(NgeDay(day=day, nge=total, n_contracts=used))
    return out


def _front_expiry(day_quotes: list[OptionQuote], day: datetime) -> datetime | None:
    """The nearest expiry strictly AFTER ``day`` (None when the day lists none)."""
    later = {q.expiry for q in day_quotes if q.expiry > day}
    return min(later) if later else None


def shift_to_next_session(series: list[NgeDay]) -> dict[str, int]:
    """The look-ahead-safe conditioning map: ISO date of day D -> sign of the LAST NGE row
    strictly BEFORE D. Keys exist only for days that HAVE a completed prior row, and the
    map's keys are the raw series' own days shifted forward one row — a fold running on a
    richer bar calendar simply finds no key on days the options tape didn't cover (no
    signal = no trade). Sign 0 (exactly-zero NGE) is kept as 0 — a "no direction" day."""
    ordered = sorted(series, key=lambda r: r.day)
    out: dict[str, int] = {}
    for prev, cur in pairwise(ordered):
        sign = 0 if prev.nge == 0 else (1 if prev.nge > 0 else -1)
        out[cur.day.date().isoformat()] = sign
    return out


def nge_decimal_settle(quote: OptionQuote) -> Decimal:
    """Test seam: the settle the pipeline prices IV from (the official daily mark)."""
    return quote.settle
