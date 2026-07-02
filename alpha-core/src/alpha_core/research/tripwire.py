"""The funding-regime tripwire (M3.0 basis follow-on) — when does the basis desk matter again?

The low-churn basis verdict (2026-07-02): hedged funding carry is REAL but regime-conditional —
the recent window simply had no funding to harvest, so nothing could pass the gate, and the
correct desk behaviour was to hold nothing. The holdout-read discipline forbids re-running the
research on a schedule, so this monitor watches the ONE number the family keys on instead: the
**trailing cross-sectional funding spread**. When the top-``top_k`` trailing annualized funding
re-crosses the family's entry threshold, the basis desk is live-relevant again — alert the
operator ([You] decides whether to re-open the research). Monitoring only: it reads LIVE public
funding, touches no research/holdout store, and deploys nothing.

Pure module: the network fetch lives in ``scripts/funding_tripwire.py``; thresholds live in
``config/research.yaml`` (no magic numbers). Money is ``Decimal``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from alpha_core.data.funding import FundingRate

_DAYS_PER_YEAR = Decimal(365)  # crypto funds every calendar day — the annual<->daily bridge


class TripwireConfig(BaseModel):
    """The ``funding_tripwire`` block of ``config/research.yaml``. Defaults mirror the basis
    family's pre-registered space: a 14-day trailing window (its lookback midpoint), the
    family's ``top_k = 8`` book breadth, and its middle entry rate (10%/yr) as the alert bar."""

    model_config = ConfigDict(extra="forbid")
    lookback_days: int = Field(default=14, gt=0)  # trailing funding window
    top_k: int = Field(default=8, gt=0)  # the basis book breadth the spread is measured over
    alert_annual_rate: Decimal = Field(default=Decimal("0.10"), gt=0)  # trigger threshold


@dataclass(frozen=True, slots=True)
class TripwireReading:
    """One assessment of the funding regime (what the alert reports)."""

    triggered: bool
    top_k_annual_rate: Decimal  # mean annualized trailing funding across the top-k names
    ranked: list[tuple[str, Decimal]]  # (symbol, annualized trailing rate), best first, top-k
    threshold: Decimal
    lookback_days: int
    symbols_assessed: int


def assess_funding_regime(
    rates_by_symbol: Mapping[str, Sequence[FundingRate]], *, config: TripwireConfig
) -> TripwireReading:
    """Assess the live funding regime from each symbol's funding events over the trailing
    ``lookback_days`` window (the caller fetches exactly that window).

    Per symbol the annualized trailing rate is ``sum(rates) / lookback_days * 365`` — summing
    is robust to any venue interval (8h/4h/1h all collapse to a per-day total). The book-level
    spread is the mean over the top-``top_k`` names; ``triggered`` when it meets the threshold
    (the basis book would be harvesting at or above the family's entry bar again)."""
    per_symbol: dict[str, Decimal] = {}
    days = Decimal(config.lookback_days)
    for symbol, rates in rates_by_symbol.items():
        if not rates:
            continue
        total = sum((r.rate for r in rates), Decimal(0))
        per_symbol[symbol] = total / days * _DAYS_PER_YEAR
    ranked = sorted(per_symbol.items(), key=lambda kv: (-kv[1], kv[0]))[: config.top_k]
    if ranked:
        top_mean = sum((rate for _, rate in ranked), Decimal(0)) / Decimal(len(ranked))
    else:
        top_mean = Decimal(0)
    return TripwireReading(
        triggered=bool(ranked) and top_mean >= config.alert_annual_rate,
        top_k_annual_rate=top_mean,
        ranked=ranked,
        threshold=config.alert_annual_rate,
        lookback_days=config.lookback_days,
        symbols_assessed=len(per_symbol),
    )


def format_reading(reading: TripwireReading) -> str:
    """The one-line-per-fact alert/report text (Telegram + logs)."""
    state = "TRIGGERED — the basis desk is live-relevant again" if reading.triggered else "quiet"
    top = ", ".join(f"{sym} {rate:.1%}" for sym, rate in reading.ranked[:5])
    return (
        f"funding tripwire: {state}. top-{len(reading.ranked)} trailing funding "
        f"{reading.top_k_annual_rate:.1%}/yr vs threshold {reading.threshold:.0%}/yr "
        f"(lookback {reading.lookback_days}d, {reading.symbols_assessed} symbols). "
        f"leaders: {top}"
    )
