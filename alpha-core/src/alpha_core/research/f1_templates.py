"""The F1 NIFTY intraday noise-breakout template registry (the intraday-mandate headline
family, `docs/research/intraday-edge-survey-2026-07.md` §5).

A **separate** registry from the nightly ``TEMPLATES`` (the ``SEASONAL_TEMPLATES`` precedent):
the family is swept by its own disciplined driver (``scripts/f1_intraday_review.py``) over the
NIFTY-index minute cell only — keeping it out of the nightly universe means a re-enabled
nightly can never silently burn this family's DSR trial budget.

The param space IS the pre-registration, survey-pinned and deliberately tiny — **4 configs,
exhaustive**: ``lookback_days`` ∈ {14, 90} (the survey's two noise-window classes, encoded as
a stepped range) x ``conditioning`` ∈ {0 = none, 1 = first-half-hour sign (Gao)}. Everything
else is family-fixed in the config model (check instants HH:00/HH:30, EOD flat 15:05 IST,
noise multiplier 1 — a knob would be a new hypothesis, not a tuning). The NGE/gamma-sign
conditioning variant from the survey requires the EOD-chain gamma pipeline and is a FUTURE
pre-registration with its own trials ledger. Widening anything here is a NEW pre-registration
— it inflates the family's trial count and must be recorded as such, never quietly edited.
"""

from __future__ import annotations

from decimal import Decimal

from alpha_core.research.strategist import DecimalRange, IntRange, StrategyTemplate
from alpha_core.strategy.examples.nifty_noise import NiftyNoiseBreakout, NiftyNoiseBreakoutConfig

F1_TEMPLATES: dict[str, StrategyTemplate] = {
    "nifty_noise_breakout": StrategyTemplate(
        "nifty_noise_breakout",
        "nifty_noise_breakout",
        NiftyNoiseBreakoutConfig,
        NiftyNoiseBreakout,
        {
            # {14, 90} — the survey's two noise windows, nothing in between.
            "lookback_days": DecimalRange(Decimal("14"), Decimal("90"), Decimal("76")),
            # 0 = unconditioned, 1 = first-half-hour sign gate (Gao).
            "conditioning": IntRange(0, 1),
        },
    ),
}
