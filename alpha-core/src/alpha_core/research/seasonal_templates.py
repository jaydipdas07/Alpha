"""The F2 seasonality template registry (the intraday-mandate family,
`docs/research/intraday-edge-survey-2026-07.md` §2.4 / §5).

A **separate** registry from the nightly ``TEMPLATES`` (the ``PANEL_TEMPLATES`` precedent): the
seasonality family is swept by its own disciplined driver (``scripts/seasonality_review.py``)
over the hourly cells only — registering it here keeps the nightly discovery universe (every
cell x every default template) untouched, so a re-enabled nightly run can never silently burn
this family's DSR trial budget on cells (5m/1d) it was never registered for.

The param spaces are the pre-registration: literature-pinned, deliberately tiny (9 configs per
template per cell), exhaustively enumerable (``max_attempts=500`` in the driver makes a missed
config astronomically unlikely). Widening a range here is a NEW pre-registration — it inflates
the family's trial count and must be recorded as such, never quietly edited.
"""

from __future__ import annotations

from alpha_core.research.strategist import IntRange, StrategyTemplate
from alpha_core.strategy.examples.seasonal_window import (
    SeasonalHourLong,
    SeasonalHourLongConfig,
    SeasonalSundayTrend,
    SeasonalSundayTrendConfig,
)

SEASONAL_TEMPLATES: dict[str, StrategyTemplate] = {
    # The late-UTC daily long window: the documented anomaly is 21-23 UTC (traditional markets
    # all closed); the space brackets it without mining the whole clock.
    "seasonal_hour_long": StrategyTemplate(
        "seasonal_hour_long",
        "seasonal_hour_long",
        SeasonalHourLongConfig,
        SeasonalHourLong,
        {"hour_start": IntRange(20, 22), "hold_hours": IntRange(2, 4)},
    ),
    # The Sunday→Monday trend window: entry bracketed around the documented Sunday-evening UTC
    # onset; direction from a 1-3 day trailing return; the 24h hold is family-fixed (config
    # default), not a searched knob — searching it would double the space for no new hypothesis.
    "seasonal_sunday_trend": StrategyTemplate(
        "seasonal_sunday_trend",
        "seasonal_sunday_trend",
        SeasonalSundayTrendConfig,
        SeasonalSundayTrend,
        {"entry_hour": IntRange(21, 23), "trend_lookback_days": IntRange(1, 3)},
    ),
}
