"""Black-76 option pricing, implied vol, and Greeks (ADR 0017 — the analytics leg).

Black-76 (the forward-measure Black model) is the Indian index-option convention:
contracts quote off the futures/forward, and with the pilot's flat zero rate the
discount factor is 1 (a rate curve slots in via ``df`` later — rho stays ``None``
until one exists, per the ``OptionGreeks`` "None, never silently 0" contract).

Boundary discipline: money/vol cross as ``Decimal``; the transcendental internals
(erf/log/exp) run in float — Greeks are ANALYTICS, not money (``core.models``),
and float noise (~1e-15) is far below any decision threshold. Time is a plain
year-fraction the CALLER computes from its injected ``now`` (nothing here reads a
clock); ``time_to_expiry`` is the shared ACT/365 helper pinned to the session
close so backtest ≡ live (TEST-1).
"""

from __future__ import annotations

import math
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from alpha_core.core.enums import OptionRight
from alpha_core.core.models import OptionGreeks

_DAYS_PER_YEAR = Decimal(365)  # ACT/365 calendar-time (crypto funds daily; simple + uniform)
# The DEFAULT settlement instant mirrors instruments.yaml's NSE session close (the
# config is authoritative — callers with a loaded schedule should pass its values;
# these defaults exist so the pure module needs no config I/O). Crypto options
# settle elsewhere (Delta: 12:00 UTC) — pass settle/tz explicitly for that leg.
_NSE_CLOSE = time(15, 30)
_NSE_TZ = ZoneInfo("Asia/Kolkata")

# Implied-vol bisection bounds/precision: [0.01%, 500%] annualized covers every
# quotable option; 80 halvings shrink the bracket below 1e-24 — exhaustive, cheap.
_IV_LO = 1e-4
_IV_HI = 5.0
_IV_ITERATIONS = 80


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def time_to_expiry(
    now: datetime,
    expiry: date,
    *,
    settle_time: time = _NSE_CLOSE,
    settle_tz: ZoneInfo = _NSE_TZ,
) -> Decimal:
    """ACT/365 year-fraction from ``now`` (tz-aware, injected) to the contract's
    settlement instant — by default the NSE cash close on the expiry date (pass
    ``settle_time``/``settle_tz`` for other venues; Delta crypto options settle
    12:00 UTC, hours off the default on the expiry day itself). Floors at zero —
    an expired contract prices as intrinsic (T=0), never a negative time."""
    if now.tzinfo is None:
        raise ValueError("now must be tz-aware (the caller owns the clock)")
    settle = datetime.combine(expiry, settle_time, tzinfo=settle_tz)
    seconds = (settle - now).total_seconds()
    if seconds <= 0:
        return Decimal(0)
    return Decimal(str(seconds)) / (_DAYS_PER_YEAR * 24 * 3600)


def _d1_d2(f: float, k: float, vol: float, t: float) -> tuple[float, float]:
    sig_rt = vol * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * vol * vol * t) / sig_rt
    return d1, d1 - sig_rt


def bs_price(
    forward: Decimal,
    strike: Decimal,
    vol: Decimal,
    t_years: Decimal,
    right: OptionRight,
    *,
    df: Decimal = Decimal(1),
) -> Decimal:
    """Black-76 premium (per unit of underlying). At ``t_years == 0`` (or zero vol)
    the price is discounted intrinsic — the settlement value, exactly."""
    if forward <= 0 or strike <= 0:
        raise ValueError("forward and strike must be positive")
    f, k, sigma, t = float(forward), float(strike), float(vol), float(t_years)
    if t <= 0 or sigma <= 0:
        intrinsic = max(f - k, 0.0) if right is OptionRight.CALL else max(k - f, 0.0)
        return df * Decimal(str(intrinsic))
    d1, d2 = _d1_d2(f, k, sigma, t)
    if right is OptionRight.CALL:
        raw = f * _norm_cdf(d1) - k * _norm_cdf(d2)
    else:
        raw = k * _norm_cdf(-d2) - f * _norm_cdf(-d1)
    return df * Decimal(str(raw))


def implied_vol(
    price: Decimal,
    forward: Decimal,
    strike: Decimal,
    t_years: Decimal,
    right: OptionRight,
    *,
    df: Decimal = Decimal(1),
) -> Decimal | None:
    """The Black-76 vol reproducing ``price``, by bisection — or ``None`` when the
    premium sits outside the no-arbitrage band (stale/crossed marks must degrade
    to "no IV", never a fabricated number; the Greeks then stay ``None`` too)."""
    if t_years <= 0 or forward <= 0 or strike <= 0 or price <= 0:
        return None
    f, k = float(forward), float(strike)
    intrinsic = max(f - k, 0.0) if right is OptionRight.CALL else max(k - f, 0.0)
    upper = f if right is OptionRight.CALL else k  # the vol→∞ premium limit
    p = float(price / df)
    if p <= intrinsic or p >= upper:
        return None
    lo, hi = _IV_LO, _IV_HI
    if not (
        float(bs_price(forward, strike, Decimal(str(lo)), t_years, right)) <= p
        and p <= float(bs_price(forward, strike, Decimal(str(hi)), t_years, right))
    ):
        return None  # outside the bracket (numerically degenerate inputs)
    for _ in range(_IV_ITERATIONS):
        mid = 0.5 * (lo + hi)
        if float(bs_price(forward, strike, Decimal(str(mid)), t_years, right)) < p:
            lo = mid
        else:
            hi = mid
    return Decimal(str(0.5 * (lo + hi)))


def greeks(
    forward: Decimal,
    strike: Decimal,
    vol: Decimal,
    t_years: Decimal,
    right: OptionRight,
    *,
    df: Decimal = Decimal(1),
) -> OptionGreeks:
    """Black-76 Greeks in the ``OptionGreeks`` raw units (vega per 1.00 vol, theta
    per YEAR — downstream scales to per-day; delta is the FORWARD delta). An
    expired/zero-vol input returns all-``None`` (nothing is differentiable there —
    "None when uncomputable, never silently 0")."""
    if t_years <= 0 or vol <= 0 or forward <= 0 or strike <= 0:
        return OptionGreeks()
    f, k, sigma, t = float(forward), float(strike), float(vol), float(t_years)
    d1, _d2 = _d1_d2(f, k, sigma, t)
    dff = float(df)
    pdf1 = _norm_pdf(d1)
    delta = dff * _norm_cdf(d1) if right is OptionRight.CALL else -dff * _norm_cdf(-d1)
    gamma = dff * pdf1 / (f * sigma * math.sqrt(t))
    vega = dff * f * pdf1 * math.sqrt(t)
    theta = -dff * f * pdf1 * sigma / (2.0 * math.sqrt(t))  # NB assumes df=1 (omits +r*C)
    return OptionGreeks(
        delta=Decimal(str(delta)),
        gamma=Decimal(str(gamma)),
        vega=Decimal(str(vega)),
        theta=Decimal(str(theta)),
        rho=None,  # needs a rate curve; None until one exists (never silently 0)
        iv=vol,  # the vol these Greeks were computed AT (the solved IV upstream)
    )
