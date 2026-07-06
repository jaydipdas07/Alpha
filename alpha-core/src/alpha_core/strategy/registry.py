"""Strategy registry — resolve a `Strategy` by name (config-driven selection).

The runner picks its strategy from `config/<env>.yaml`'s `strategy:` value rather
than importing a concrete class, so swapping the edge is a one-line config change
(no code edit). To add a strategy: implement `Strategy` and register one builder
here. A new trading edge slots in without touching the runner.
"""

from __future__ import annotations

from collections.abc import Callable

from alpha_core.core.interfaces import Strategy
from alpha_core.helpers.config import ConfigError
from alpha_core.strategy.examples.bollinger_squeeze import BollingerSqueeze
from alpha_core.strategy.examples.donchian_atr import DonchianAtr
from alpha_core.strategy.examples.idle import IdleStrategy
from alpha_core.strategy.examples.ma_crossover import MaCrossover
from alpha_core.strategy.examples.macd import Macd
from alpha_core.strategy.examples.momentum_roc import MomentumRoc
from alpha_core.strategy.examples.nifty_noise import NiftyNoiseBreakout
from alpha_core.strategy.examples.opening_range_breakout import OpeningRangeBreakout
from alpha_core.strategy.examples.placeholder import PlaceholderStrategy
from alpha_core.strategy.examples.rsi_bollinger import RsiBollinger
from alpha_core.strategy.examples.seasonal_window import SeasonalHourLong, SeasonalSundayTrend
from alpha_core.strategy.examples.trend_pullback import TrendPullback
from alpha_core.strategy.examples.vwap_reversion import VwapReversion

# name (as written in config) -> zero-arg builder. Two Vega strategies stay deferred until
# their layer lands: `vertical_spread` (an OPTION strategy, needs the options layer) and
# `kill_test` (a live kill-switch drill, Phase 3) — register them when those arrive.
_BUILDERS: dict[str, Callable[[], Strategy]] = {
    "opening_range_breakout": OpeningRangeBreakout.from_config,
    # The common-strategy set (R7) — the strategist's vetted seed templates (B1b.1).
    "ma_crossover": MaCrossover.from_config,
    "rsi_bollinger": RsiBollinger.from_config,
    "momentum_roc": MomentumRoc.from_config,
    "vwap_reversion": VwapReversion.from_config,
    # Richer templates (M3.0) — EMA momentum, volatility-regime breakouts, multi-signal confluence.
    "macd": Macd.from_config,
    "donchian_atr": DonchianAtr.from_config,
    "bollinger_squeeze": BollingerSqueeze.from_config,
    "trend_pullback": TrendPullback.from_config,
    # F2 seasonality (the intraday-mandate family) — config-driven deployment of a survivor.
    "seasonal_hour_long": SeasonalHourLong.from_config,
    "seasonal_sunday_trend": SeasonalSundayTrend.from_config,
    # F1 NIFTY noise-area breakout (the intraday-mandate headline family).
    "nifty_noise_breakout": NiftyNoiseBreakout.from_config,
    "placeholder": PlaceholderStrategy,
    "idle": IdleStrategy,  # no-trade: infra soak / live smoke check
}


def build_strategy(name: str) -> Strategy:
    """Build the named strategy, or fail fast naming the known set."""
    builder = _BUILDERS.get(name)
    if builder is None:
        raise ConfigError(f"unknown strategy {name!r}; known: {sorted(_BUILDERS)}")
    return builder()


def known_strategies() -> list[str]:
    """The registered strategy names (config-selectable). Read-only accessor for
    operators/tools (e.g. the control dashboard) — does not build anything."""
    return sorted(_BUILDERS)
