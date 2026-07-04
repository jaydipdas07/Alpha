"""Execution cost *scenarios* for the research plane — "taker" vs "maker" pricing, one home.

The basis family set the convention (``basis_backtester.basis_cost_fraction``): a scenario is
a *named execution assumption*, and every number still comes from ``config/costs.yaml`` — no
magic numbers, one config home. This module generalizes it for the other two research paths:

- :func:`cost_per_side` — the flat per-side cost fraction the tick-scale folds charge
  (lead-lag, liquidation, funding-window). ``"taker"`` = ``trading_fee`` + bps slippage (the
  deployable-today assumption, identical to ``taker_cost_per_side``); ``"maker"`` =
  ``maker_fee`` only — a resting post-only order pays no crossing slippage.
- :func:`scenario_cost_config` — a pure transform of the parsed ``costs.yaml`` mapping for
  the **engine** path (``EngineBacktester`` → ``run_backtest`` → ``CostModel``): under
  ``"maker"`` the crypto-perp ``trading_fee`` is repriced at the ``maker_fee`` rate and the
  crypto-perp slippage + LTP default-spread legs are zeroed (a resting order doesn't cross).
  The input mapping is never mutated.

**Honesty contract (mirrors the basis wording):** the maker scenario models FEES only.
Fill uncertainty and adverse selection are unmodelled, so maker-scenario results are
*in-sample evidence about a maker-execution deployment* — an ability the worker does not
have yet (Phase-4+, post-only order type + a fill model). Per ``promote.py``: any holdout
read must run under the scenario of the *intended deployment*, and a maker-only survivor
must be labelled as such everywhere it is reported.
"""

from __future__ import annotations

import copy
from typing import Any, Literal, cast

from alpha_core.helpers.config import load_yaml

CostScenario = Literal["taker", "maker"]
_SCENARIOS: tuple[CostScenario, ...] = ("taker", "maker")


def _require_scenario(scenario: str) -> None:
    if scenario not in _SCENARIOS:
        raise ValueError(f"unknown cost scenario {scenario!r}; known: {', '.join(_SCENARIOS)}")


def cost_per_side(scenario: str = "taker") -> float:
    """Per-side cost fraction for the tick-scale folds under ``scenario`` (crypto_perp).

    ``"taker"``: ``trading_fee.pct`` + bps slippage — same number as
    ``funding_window_backtester.taker_cost_per_side`` (which delegates here).
    ``"maker"``: ``maker_fee.pct`` only (post-only resting order — no crossing slippage;
    fill risk unmodelled, see module docstring).
    """
    _require_scenario(scenario)
    cfg = load_yaml("costs.yaml")
    segment = cast(dict[str, dict[str, object]], cfg["segments"])["crypto_perp"]
    if scenario == "maker":
        if "maker_fee" not in segment:
            raise ValueError("costs.yaml segments.crypto_perp has no maker_fee (required)")
        return float(cast(float, cast(dict[str, object], segment["maker_fee"])["pct"]))
    trading = cast(dict[str, object], segment["trading_fee"])
    slippage = cast(dict[str, dict[str, object]], cfg["slippage"])["crypto_perp"]
    if slippage.get("type") != "bps":  # the /10000 below assumes bps — fail loud if retuned
        raise ValueError(f"slippage.crypto_perp.type must be 'bps', got {slippage.get('type')!r}")
    return float(cast(float, trading["pct"])) + float(cast(int, slippage["value"])) / 10_000.0


def scenario_cost_config(cost_config: dict[str, Any], scenario: str = "taker") -> dict[str, Any]:
    """The parsed ``costs.yaml`` mapping repriced for ``scenario`` (engine path).

    ``"taker"`` returns the input unchanged (same object — the today-default is a no-op).
    ``"maker"`` returns a DEEP COPY where, for the crypto-perp segment only:

    - ``segments.crypto_perp.trading_fee.pct`` ← ``maker_fee.pct`` (the fee the ``CostModel``
      actually reads; ``maker_fee`` itself is outside its component allowlist),
    - ``slippage.crypto_perp.value`` ← 0 (no adverse crossing buffer for a resting order),
    - ``slippage.default_spread.crypto_perp`` ← 0 (LTP-only bars synthesize a spread to
      cross; a resting order doesn't cross it).

    Only the crypto-perp keys are touched — equity/options segments pass through, so a
    mixed-universe run fails no differently than today. Raises ``ValueError`` if the segment
    has no ``maker_fee`` (a scenario must never silently price as taker).
    """
    _require_scenario(scenario)
    if scenario == "taker":
        return cost_config
    out = copy.deepcopy(cost_config)
    segment = out.get("segments", {}).get("crypto_perp")
    if not isinstance(segment, dict) or "maker_fee" not in segment:
        raise ValueError("maker scenario needs segments.crypto_perp.maker_fee in costs.yaml")
    segment["trading_fee"] = {
        **cast(dict[str, Any], segment["trading_fee"]),
        "pct": cast(dict[str, Any], segment["maker_fee"])["pct"],
    }
    slip = out.get("slippage", {})
    if "crypto_perp" in slip:
        slip["crypto_perp"] = {**slip["crypto_perp"], "value": 0}
    if "default_spread" in slip and "crypto_perp" in slip["default_spread"]:
        slip["default_spread"]["crypto_perp"] = 0
    return out
