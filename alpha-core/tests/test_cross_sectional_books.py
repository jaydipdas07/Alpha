"""The richer cross-sectional book constructions (M3.0 follow-on): inverse-vol leg weights and the
benchmark-beta hedge. Crown-jewel checks: the vol-scaled legs stay ±1 with the high-vol name
underweighted, and the beta-neutral book's ex-ante benchmark beta is ~0 on a synthetic
benchmark-driven panel."""

from __future__ import annotations

from decimal import Decimal

import pytest

from alpha_core.research.panel_backtester import PANEL_TEMPLATES
from alpha_core.strategy.examples.cross_sectional import (
    BetaNeutralConfig,
    BetaNeutralMomentum,
    CrossSectionalConfig,
    VolScaledMomentum,
    VolScaledReversal,
    trailing_beta,
)


def _seq(d: dict[str, list[str]]) -> dict[str, list[Decimal]]:
    return {k: [Decimal(x) for x in v] for k, v in d.items()}


# --- vol-scaled legs -------------------------------------------------------------------------


def test_vol_scaled_legs_stay_dollar_neutral_and_underweight_the_high_vol_name() -> None:
    # top_k=2 with 6 names; A and B share the SAME trailing return (both x8 over lookback=3) but B
    # gets there in one violent bar — higher vol, so the smaller |weight| within the long leg.
    closes = _seq(
        {
            "A": ["1", "2", "3.8", "8"],  # near-steady doubling -> low bar-return vol
            "B": ["1", "1", "1", "8"],  # one x8 jump -> much higher bar-return vol
            "C": ["1", "1", "1", "1"],
            "D": ["1", "1", "1", "1.1"],
            "E": ["8", "4.2", "2", "1"],  # near-steady loser -> low vol
            "F": ["8", "8", "8", "1"],  # violent loser
        }
    )
    weights = VolScaledMomentum(CrossSectionalConfig(lookback=3, top_k=2)).target_weights(closes)
    longs = {s: w for s, w in weights.items() if w > 0}
    shorts = {s: w for s, w in weights.items() if w < 0}
    assert set(longs) == {"A", "B"} and set(shorts) == {"E", "F"}  # same SELECTION as the base rank
    assert sum(longs.values()) == pytest.approx(Decimal(1))  # each leg still fully invested...
    assert sum(shorts.values()) == pytest.approx(Decimal(-1))
    assert longs["B"] < longs["A"]  # ...but risk-parity within it: the violent name carries less
    assert abs(shorts["F"]) < abs(shorts["E"])


def test_vol_scaled_falls_back_to_equal_weight_on_a_degenerate_flat_leg() -> None:
    # E is flat over the vol window (sigma=0): inverse-vol would blow up, so THAT leg falls back to
    # equal weight (deterministic; a real market series never has exactly zero vol).
    closes = _seq(
        {
            "A": ["1", "2", "4", "8"],
            "B": ["1", "1", "1", "8"],
            "C": ["1", "1", "1", "1.2"],
            "D": ["1", "1", "1", "1.1"],
            "E": ["2", "2", "2", "1"],  # loser leg member, flat until the final drop
            "F": ["8", "4", "2", "1"],
        }
    )
    # E's vol window (lookback=1 -> 1 return) is too short for a stdev -> fallback path.
    weights = VolScaledMomentum(CrossSectionalConfig(lookback=1, top_k=2)).target_weights(closes)
    shorts = {s: w for s, w in weights.items() if w < 0}
    assert set(shorts) == {"E", "F"}
    assert shorts["E"] == shorts["F"] == Decimal("-0.5")  # equal weight, leg sum still -1


def test_vol_scaled_reversal_flips_the_selection() -> None:
    closes = _seq(
        {
            "A": ["1", "2", "3.8", "8"],
            "B": ["1", "1", "1", "8"],
            "C": ["1", "1", "1", "1"],
            "D": ["1", "1", "1", "1.1"],
            "E": ["8", "4.2", "2", "1"],
            "F": ["8", "8", "8", "1"],
        }
    )
    momentum = VolScaledMomentum(CrossSectionalConfig(lookback=3, top_k=2)).target_weights(closes)
    reversal = VolScaledReversal(CrossSectionalConfig(lookback=3, top_k=2)).target_weights(closes)
    assert {s for s, w in reversal.items() if w > 0} == {s for s, w in momentum.items() if w < 0}


# --- the benchmark-beta hedge ----------------------------------------------------------------


def _beta_panel() -> dict[str, list[Decimal]]:
    """Members that move as beta x the benchmark (BTCUSDT alternates ±10%): A beta 2 + a winner's
    drift, B beta 1, C beta 0.5, D beta 1 - a loser's drift."""
    btc = [Decimal(100)]
    for i in range(6):
        btc.append(btc[-1] * (Decimal("1.1") if i % 2 == 0 else Decimal("0.9")))
    panel: dict[str, list[Decimal]] = {"BTCUSDT": btc}
    for sym, beta, drift in (
        ("A", Decimal(2), Decimal("0.05")),
        ("B", Decimal(1), Decimal("0.01")),
        ("C", Decimal("0.5"), Decimal("-0.01")),
        ("D", Decimal(1), Decimal("-0.05")),
    ):
        series = [Decimal(100)]
        for i in range(6):
            r_btc = btc[i + 1] / btc[i] - Decimal(1)
            series.append(series[-1] * (Decimal(1) + beta * r_btc + drift))
        panel[sym] = series
    return panel


def test_trailing_beta_recovers_the_constructed_beta() -> None:
    panel = _beta_panel()
    beta = trailing_beta(panel["A"], panel["BTCUSDT"], 6)
    assert beta is not None and float(beta) == pytest.approx(2.0, abs=1e-9)
    flat = [Decimal(100)] * 7
    assert trailing_beta(panel["A"], flat, 6) == Decimal(0)  # flat benchmark -> nothing to hedge
    assert trailing_beta(panel["A"][:3], panel["BTCUSDT"], 6) is None  # too short


def test_beta_neutral_book_has_zero_ex_ante_benchmark_beta() -> None:
    panel = _beta_panel()
    weights = BetaNeutralMomentum(BetaNeutralConfig(lookback=6, top_k=1)).target_weights(panel)
    assert weights and "BTCUSDT" in weights  # the hedge leg is present
    book_beta = (
        sum(
            w * (trailing_beta(panel[s], panel["BTCUSDT"], 6) or Decimal(0))
            for s, w in weights.items()
            if s != "BTCUSDT"
        )
        + weights["BTCUSDT"]
    )  # the benchmark's own beta to itself is 1
    assert float(book_beta) == pytest.approx(0.0, abs=1e-9)


def test_beta_neutral_takes_no_book_without_its_hedge() -> None:
    panel = {s: series for s, series in _beta_panel().items() if s != "BTCUSDT"}
    assert BetaNeutralMomentum(BetaNeutralConfig(lookback=6, top_k=1)).target_weights(panel) == {}


def test_beta_neutral_hedge_symbol_is_config() -> None:
    panel = dict(_beta_panel())
    panel["ETHUSDT"] = panel.pop("BTCUSDT")
    cfg = BetaNeutralConfig(lookback=6, top_k=1, hedge_symbol="ETHUSDT")
    weights = BetaNeutralMomentum(cfg).target_weights(panel)
    assert weights and "ETHUSDT" in weights


# --- registry ---------------------------------------------------------------------------------


def test_richer_books_are_registered_panel_templates() -> None:
    for name in (
        "cross_sectional_momentum_volscaled",
        "cross_sectional_reversal_volscaled",
        "cross_sectional_momentum_betaneutral",
    ):
        template = PANEL_TEMPLATES[name]
        built = template.build({"lookback": 5, "top_k": 2, "holding_period": 1})
        assert built.config.lookback == 5  # type: ignore[attr-defined]
