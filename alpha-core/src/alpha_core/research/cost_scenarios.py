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


def futures_costed_equity_config(cost_config: dict[str, Any]) -> dict[str, Any]:
    """The parsed ``costs.yaml`` repriced so EQUITY-mapped instruments charge the
    **index-futures** stack — the F1 overlay (survey §5: "NIFTY-futures cost overlay").

    The F1 research cell is the NIFTY 50 *index* under ``AssetClass.EQUITY`` (the engine's
    only cash-equity mapping), but the deployable instrument is the near-month future —
    charging the cash-equity stack would UNDERSTATE the deployable STT (0.025% vs the
    futures 0.05% sell-side, Budget-2026). Returns a DEEP COPY with, for the equity keys
    only: ``segments.equity_intraday`` <- ``segments.index_future``,
    ``slippage.equity`` <- ``slippage.index_future``, and
    ``slippage.default_spread.equity`` <- ``default_spread.index_future``. Raises
    ``ValueError`` when the ``index_future`` homes are absent (the overlay must never
    silently price as cash equity).

    **Honesty note:** the fold marks at INDEX levels; intraday index-vs-future basis
    drift is unmodelled (small at minute scale, nonzero) — declared in the F1
    pre-registration, revisited before any paper deployment.
    """
    out = copy.deepcopy(cost_config)
    segments = out.get("segments", {})
    if "index_future" not in segments:
        raise ValueError("futures overlay needs segments.index_future in costs.yaml")
    segments["equity_intraday"] = copy.deepcopy(segments["index_future"])
    slip = out.get("slippage", {})
    if "index_future" not in slip or "index_future" not in slip.get("default_spread", {}):
        raise ValueError(
            "futures overlay needs slippage.index_future + default_spread.index_future"
        )
    slip["equity"] = copy.deepcopy(slip["index_future"])
    slip["default_spread"]["equity"] = slip["default_spread"]["index_future"]
    return out


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


def _intraday_stack_sides(segment_key: str, slippage_key: str) -> tuple[float, float]:
    """(buy_side, sell_side) cost fractions for one Indian intraday statute stack:
    ``segments.<segment_key>`` percentage legs + ``slippage.<slippage_key>`` (bps).

    Percentage legs only — ``brokerage.flat`` (min(0.03 %, ₹20)/order) is size-dependent
    and the pct leg is the CONSERVATIVE bound (the flat cap only lowers the rate above
    ~₹67k/order), so signal-scale folds charge 0.03 %. GST applies to the ``"on"`` list
    (the #177 quoting fix); STT is sell-side, stamp duty buy-side. Raises when a leg is
    missing — a repriced costs.yaml must never silently undercharge.
    """
    cfg = load_yaml("costs.yaml")
    seg = cast(dict[str, dict[str, Any]], cfg["segments"])[segment_key]
    slip = cast(dict[str, dict[str, Any]], cfg["slippage"])[slippage_key]
    if slip.get("type") != "bps":
        raise ValueError(f"slippage.{slippage_key}.type must be 'bps', got {slip.get('type')!r}")
    brokerage = float(cast(float, seg["brokerage"]["pct"]))
    exchange = float(cast(float, seg["exchange_txn"]["pct"]))
    sebi = float(cast(float, seg["sebi"]["pct"]))
    gst_leg_names = cast(list[str], seg["gst"]["on"])
    gst_base = sum(float(cast(float, seg[leg]["pct"])) for leg in gst_leg_names)
    gst = float(cast(float, seg["gst"]["pct"])) * gst_base
    slippage = float(cast(int, slip["value"])) / 10_000.0
    common = brokerage + exchange + sebi + gst + slippage
    buy = common + float(cast(float, seg["stamp_duty"]["pct"]))
    sell = common + float(cast(float, seg["stt"]["pct"]))
    return buy, sell


def equity_intraday_cost_sides() -> tuple[float, float]:
    """The CASH-EQUITY MIS stack (the G1/G3 panel families): ``segments.equity_intraday``
    + ``slippage.equity`` — see ``_intraday_stack_sides`` for the leg semantics."""
    return _intraday_stack_sides("equity_intraday", "equity")


def index_future_cost_sides() -> tuple[float, float]:
    """The INDEX-FUTURES stack (the G4 max-pain family's deployable instrument, the F1
    overlay precedent): ``segments.index_future`` + ``slippage.index_future`` — same leg
    semantics. The fold marks at INDEX levels; intraday index-vs-future basis drift is
    unmodelled (declared per registration, revisited before any paper deployment)."""
    return _intraday_stack_sides("index_future", "index_future")
