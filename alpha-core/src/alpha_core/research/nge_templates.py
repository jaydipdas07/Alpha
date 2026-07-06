"""The NGE/last-half-hour-momentum template registry (the Baltussen-form family —
`docs/research/intraday-edge-survey-2026-07.md` §5's gamma-conditioning axis).

A separate registry (the ``SEASONAL_TEMPLATES``/``F1_TEMPLATES`` precedent), built by a
FACTORY because the conditioned variant needs the NGE sign map injected at construction
(``research/nge.py`` — computed from the options RESEARCH partition only, already shifted
one session so the strategy's same-day lookup is look-ahead-safe by construction).

**The pre-registration — 2 configs, exhaustive:** ``conditioning`` ∈ {0 = unconditioned,
1 = short-gamma days only}. Everything else is family-fixed in the config model (signal
14:35 IST, flat 15:05, quantity constant). The registered fold window is the OVERLAP of the
equity minute cell and the options research partition (~2016-02→2024-05-24, the driver
bounds it); the family's holdout read is DEFERRED — it would need both a powered equity
window (the interval floor thins it today) and a sanctioned options-holdout NGE computation
(the recorded cross-family coupling). Widening anything here is a NEW pre-registration.
"""

from __future__ import annotations

from collections.abc import Mapping

from alpha_core.research.strategist import IntRange, StrategyTemplate
from alpha_core.strategy.examples.nifty_lhh import NiftyLhhMomentum, NiftyLhhMomentumConfig


def nge_templates(nge_sign_for_day: Mapping[str, int]) -> dict[str, StrategyTemplate]:
    """Build the family registry with the conditioning map closed over the builder.

    The proposal params stay tiny and fingerprintable ({conditioning}); the map's
    provenance (row count, span) is pinned by the driver in the run log."""

    def _build(config: NiftyLhhMomentumConfig) -> NiftyLhhMomentum:
        return NiftyLhhMomentum(config, nge_sign_for_day=nge_sign_for_day)

    return {
        "nifty_lhh_momentum": StrategyTemplate(
            "nifty_lhh_momentum",
            "nifty_lhh_momentum",
            NiftyLhhMomentumConfig,
            _build,
            {"conditioning": IntRange(0, 1)},
        ),
    }
