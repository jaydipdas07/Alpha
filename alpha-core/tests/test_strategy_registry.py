"""Strategy registry — config-driven strategy selection (ADR 0013); the strategist's seed
templates (B1b.1). `vertical_spread` (options) + `kill_test` (Phase 3) stay deferred."""

from __future__ import annotations

import pytest

from alpha_core.core.interfaces import Strategy
from alpha_core.helpers.config import ConfigError
from alpha_core.strategy.registry import build_strategy, known_strategies


def test_build_known_strategies() -> None:
    for name in known_strategies():
        assert isinstance(build_strategy(name), Strategy)


def test_the_r7_seed_templates_are_registered() -> None:
    # the vetted trading templates the strategist parameterizes (R7).
    for name in (
        "ma_crossover",
        "rsi_bollinger",
        "momentum_roc",
        "vwap_reversion",
        "opening_range_breakout",
    ):
        assert name in known_strategies()
        assert isinstance(build_strategy(name), Strategy)


def test_deferred_strategies_are_not_registered() -> None:
    # vertical_spread (needs the options layer) + kill_test (Phase 3) are deferred.
    for deferred in ("vertical_spread", "kill_test"):
        assert deferred not in known_strategies()
        with pytest.raises(ConfigError, match="unknown strategy"):
            build_strategy(deferred)


def test_unknown_strategy_fails_fast() -> None:
    with pytest.raises(ConfigError, match="unknown strategy"):
        build_strategy("no_such_strategy")
