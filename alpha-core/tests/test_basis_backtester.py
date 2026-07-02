"""The delta-neutral basis-carry path (M3.0 basis track) — the selection strategy, the two-leg
fold, the config plumbing, and the TEST-3 wiring. The crown-jewel check: with BOTH legs on the
SAME trending price path the book's return is *pure carry* (the direction cancels per name) —
exactly the price risk that drowned the unhedged carry family."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.funding import FundingRate, FundingStore
from alpha_core.data.holdout import HoldoutStore
from alpha_core.data.store import BarStore
from alpha_core.helpers.config import (
    DiscoveryBasisConfig,
    DiscoveryCellConfig,
    DiscoveryConfig,
    DiscoveryPanelConfig,
)
from alpha_core.research.basis_backtester import (
    BASIS_TEMPLATES,
    BasisPanelBacktester,
    spot_turnover_cost_fraction,
)
from alpha_core.research.cold_store_bars import CellKey
from alpha_core.research.holdout_gate import HoldoutPanelBarsFor
from alpha_core.research.panel_backtester import ColdStorePanelBarsFor
from alpha_core.research.promote import build_basis_backtesters
from alpha_core.research.strategist import StrategyProposal
from alpha_core.strategy.examples.cross_sectional import BasisCarry, CrossSectionalConfig

DAY = timedelta(days=1)
START = datetime(2024, 1, 1, tzinfo=UTC)
_CRYPTO = AssetClass.CRYPTO
_CELL = "crypto-basis-1d"
_PERP = "perps"
_SPOT = "spot"


def _seq(d: dict[str, list[str]]) -> dict[str, list[Decimal]]:
    return {k: [Decimal(x) for x in v] for k, v in d.items()}


def _bars(symbol: str, closes: list[str], venue: Venue, offset: int = 0) -> list[Bar]:
    """A flat-OHLC daily series at consecutive UTC starts on ``venue`` (perp vs spot leg)."""
    out: list[Bar] = []
    for i, c in enumerate(closes):
        price = Decimal(c)
        out.append(
            Bar(
                symbol=symbol,
                venue=venue,
                asset_class=_CRYPTO,
                start=START + (offset + i) * DAY,
                interval=DAY,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=Decimal(1),
            )
        )
    return out


def _funding(symbol: str, rates: list[str]) -> list[FundingRate]:
    """One funding payment per UTC day at midnight (daily_funding keys match the bar starts)."""
    return [
        FundingRate(
            symbol=symbol, venue=Venue.BINANCE, funding_time=START + i * DAY, rate=Decimal(r)
        )
        for i, r in enumerate(rates)
    ]


def _proposal(
    params: dict[str, int], template: str = "basis_carry", window: str = _CELL
) -> StrategyProposal:
    return StrategyProposal(template, params, _CRYPTO, window, 1, "fp")


def _bt(
    perp: dict[str, list[Bar]],
    spot: dict[str, list[Bar]],
    funding: dict[str, list[FundingRate]],
    *,
    cost: Decimal = Decimal(0),
    min_bars: int = 1,
) -> BasisPanelBacktester:
    """A backtester over injected two-leg panels: ``panel_bars_for`` dispatches on the leg name."""
    legs = {_PERP: perp, _SPOT: spot}
    cells: dict[CellKey, tuple[str, str]] = {(_CRYPTO, _CELL): (_PERP, _SPOT)}
    return BasisPanelBacktester(
        panel_bars_for=lambda _m, window: legs[window],
        panel_funding_for=lambda _m, _w: funding,
        basis_cells=cells,
        min_bars=min_bars,
        cost_fraction=cost,
    )


_PARAMS = {"lookback": 1, "top_k": 1, "holding_period": 1}


# --- the BasisCarry selection strategy ------------------------------------------------------------


def test_basis_carry_selects_top_positive_funding_equal_weight() -> None:
    funding = _seq({"A": ["0.001"], "B": ["0.0005"], "C": ["0.0002"]})
    weights = BasisCarry(CrossSectionalConfig(lookback=1, top_k=2)).target_weights(funding)
    assert weights == {"A": Decimal("0.5"), "B": Decimal("0.5")}


def test_basis_carry_goes_flat_when_no_positive_carry() -> None:
    # a negative-funding basis position PAYS funding — the desk holds nothing rather than bleed.
    funding = _seq({"A": ["-0.001"], "B": ["0"]})
    assert BasisCarry(CrossSectionalConfig(lookback=1, top_k=2)).target_weights(funding) == {}


def test_basis_carry_holds_a_smaller_book_when_few_qualify() -> None:
    # only A is positive: the book is just A at full weight (1/m over the m that qualify).
    funding = _seq({"A": ["0.001"], "B": ["-0.001"], "C": ["-0.002"]})
    weights = BasisCarry(CrossSectionalConfig(lookback=1, top_k=3)).target_weights(funding)
    assert weights == {"A": Decimal(1)}


def test_basis_carry_excludes_too_short_history_and_breaks_ties_by_symbol() -> None:
    funding = _seq({"A": ["0.001"] * 3, "B": ["0.001"] * 3, "C": ["0.002"]})  # C: 1 < lookback 3
    weights = BasisCarry(CrossSectionalConfig(lookback=3, top_k=1)).target_weights(funding)
    assert weights == {"A": Decimal(1)}  # tie A/B -> symbol ascending picks A; C unscoreable


# --- the two-leg fold: delta-neutrality, carry timing, costs, membership -------------------------


def test_return_is_pure_carry_when_both_legs_trend_together() -> None:
    # THE crown jewel: both legs ride the SAME +10%/day path, so the short perp exactly cancels
    # the long spot and the book earns only the funding — the price risk that drowned the
    # unhedged carry family is gone by construction.
    n = 6
    path = [str(Decimal("100") * (Decimal("1.1") ** i)) for i in range(n)]
    perp = {s: _bars(s, path, Venue.BINANCE) for s in ("A", "B")}
    spot = {s: _bars(s, path, Venue.BINANCE_SPOT) for s in ("A", "B")}
    funding = {"A": _funding("A", ["0.001"] * n), "B": _funding("B", ["0.0005"] * n)}
    returns = list(_bt(perp, spot, funding).run(_proposal(_PARAMS)))
    # A (highest positive funding) is held throughout; carry = +0.001/day, direction cancelled.
    assert returns and all(r == pytest.approx(0.001) for r in returns)


def test_basis_move_is_spot_minus_perp() -> None:
    # spot gaps +1% on day 1->2 while the perp stays flat: that bar earns the basis move + carry.
    perp = {"A": _bars("A", ["100"] * 4, Venue.BINANCE)}
    spot = {"A": _bars("A", ["100", "100", "101", "101"], Venue.BINANCE_SPOT)}
    funding = {"A": _funding("A", ["0.001"] * 4)}
    returns = list(_bt(perp, spot, funding).run(_proposal(_PARAMS)))
    # bar 1->2 spans the gap: (101/100 - 100/100) + 0.001; bar 2->3 is carry-only again.
    assert returns == [pytest.approx(0.011), pytest.approx(0.001)]


def test_carry_uses_next_day_funding_not_the_current_day() -> None:
    # rising funding pins the i+1 timing: flat prices, A always selected -> returns must be the
    # NEXT day's funding (a regression to funding[i] would shift the series one slot down).
    rates = ["0.001", "0.002", "0.003", "0.004", "0.005", "0.006"]
    perp = {"A": _bars("A", ["100"] * 6, Venue.BINANCE)}
    spot = {"A": _bars("A", ["100"] * 6, Venue.BINANCE_SPOT)}
    funding = {"A": _funding("A", rates)}
    returns = list(_bt(perp, spot, funding).run(_proposal(_PARAMS)))
    assert returns == [pytest.approx(x) for x in (0.003, 0.004, 0.005, 0.006)]


def test_two_leg_turnover_cost_charged_at_rebalance() -> None:
    n = 5
    perp = {"A": _bars("A", ["100"] * n, Venue.BINANCE)}
    spot = {"A": _bars("A", ["100"] * n, Venue.BINANCE_SPOT)}
    funding = {"A": _funding("A", ["0.001"] * n)}
    cost = Decimal("0.0023")  # the COMBINED both-leg rate per unit of book turnover
    returns = list(_bt(perp, spot, funding, cost=cost).run(_proposal(_PARAMS)))
    # entry turnover 1 on the first rebalance; the selection never changes after -> no more cost.
    assert returns[0] == pytest.approx(0.001 - 0.0023)
    assert returns[1:] == [pytest.approx(0.001)] * (len(returns) - 1)


def test_a_name_needs_both_legs_to_be_selectable() -> None:
    # A has the best funding but NO spot leg -> unhedgeable -> B (complete) is selected instead.
    n = 5
    perp = {s: _bars(s, ["100"] * n, Venue.BINANCE) for s in ("A", "B")}
    spot = {"B": _bars("B", ["100"] * n, Venue.BINANCE_SPOT)}
    funding = {"A": _funding("A", ["0.005"] * n), "B": _funding("B", ["0.001"] * n)}
    returns = list(_bt(perp, spot, funding).run(_proposal(_PARAMS)))
    assert returns and all(r == pytest.approx(0.001) for r in returns)  # B's carry, not A's


def test_a_leg_gap_mid_hold_contributes_flat_zero() -> None:
    # hold=4: A is selected at i=1 and held; its SPOT leg stops printing after day 2, so bars
    # 2->3 and 3->4 cannot be marked -> flat 0 those bars (never a fill, never a phantom carry).
    perp = {"A": _bars("A", ["100"] * 6, Venue.BINANCE)}
    spot = {"A": _bars("A", ["100", "100", "100"], Venue.BINANCE_SPOT)}  # days 0..2 only
    funding = {"A": _funding("A", ["0.001"] * 6)}
    params = {"lookback": 1, "top_k": 1, "holding_period": 4}
    returns = list(_bt(perp, spot, funding).run(_proposal(params)))
    # i=1 (rebalance): bars 1->2 real on both legs -> carry; i=2..4 (still holding, no re-rank
    # due yet): the spot leg has no bar -> the name cannot be marked -> flat 0 each bar.
    assert returns == [pytest.approx(0.001), 0.0, 0.0, 0.0]


def test_lookahead_perturbing_a_future_bar_leaves_earlier_returns_unchanged() -> None:
    n = 8
    perp = {s: _bars(s, ["100"] * n, Venue.BINANCE) for s in ("A", "B")}
    spot_base = {s: _bars(s, ["100"] * n, Venue.BINANCE_SPOT) for s in ("A", "B")}
    funding = {"A": _funding("A", ["0.002"] * n), "B": _funding("B", ["0.001"] * n)}
    base = list(_bt(perp, spot_base, funding).run(_proposal(_PARAMS)))
    spot_bumped = dict(spot_base)
    spot_bumped["A"] = _bars("A", ["100"] * (n - 1) + ["150"], Venue.BINANCE_SPOT)
    bumped = list(_bt(perp, spot_bumped, funding).run(_proposal(_PARAMS)))
    assert bumped[:-1] == base[:-1]  # only the final bar (which spans the bump) may differ
    assert bumped[-1] != base[-1]


def test_unknown_template_and_unknown_cell_raise() -> None:
    perp = {"A": _bars("A", ["100"] * 5, Venue.BINANCE)}
    spot = {"A": _bars("A", ["100"] * 5, Venue.BINANCE_SPOT)}
    funding = {"A": _funding("A", ["0.001"] * 5)}
    bt = _bt(perp, spot, funding)
    with pytest.raises(ValueError, match="unknown basis template"):
        bt.run(_proposal(_PARAMS, template="cross_sectional_momentum"))
    with pytest.raises(ValueError, match="no basis cell mapped"):
        bt.run(_proposal(_PARAMS, window="not-a-cell"))


def test_too_few_aligned_bars_raises_the_rigor_floor() -> None:
    perp = {"A": _bars("A", ["100"] * 4, Venue.BINANCE)}
    spot = {"A": _bars("A", ["100"] * 4, Venue.BINANCE_SPOT)}
    funding = {"A": _funding("A", ["0.001"] * 4)}
    with pytest.raises(ValueError, match="too few aligned in-sample bars"):
        _bt(perp, spot, funding, min_bars=10).run(_proposal(_PARAMS))


def test_spot_turnover_cost_reads_the_spot_regime() -> None:
    # crypto_spot fee (10 bps) + spot slippage (8 bps) — and NOT the TDS withholding (a
    # creditable deploy-time cash flow, deliberately excluded from the research fold).
    assert spot_turnover_cost_fraction() == Decimal("0.0008") + Decimal("0.001")


# --- template registry + TEST-3 wiring ------------------------------------------------------------


def test_basis_template_registered_and_buildable() -> None:
    template = BASIS_TEMPLATES["basis_carry"]
    strategy = template.build({"lookback": 5, "top_k": 3, "holding_period": 2})
    assert isinstance(strategy, BasisCarry)
    assert strategy.config.lookback == 5


def test_build_basis_backtesters_wires_the_test3_boundary(tmp_path: object) -> None:
    # the load-bearing TEST-3 wiring: BOTH legs of the in-sample fold read the research store,
    # BOTH legs of the holdout fold the gate-only store — one boundary object per side.
    base = Path(str(tmp_path))
    in_sample, holdout_bt = build_basis_backtesters(
        research_store=BarStore(base / "research"),
        holdout_store=HoldoutStore(base / "holdout"),
        funding_store=FundingStore(base / "funding"),
    )
    assert isinstance(in_sample, BasisPanelBacktester)
    assert isinstance(holdout_bt, BasisPanelBacktester)
    assert isinstance(in_sample._panel_bars_for, ColdStorePanelBarsFor)
    assert isinstance(holdout_bt._panel_bars_for, HoldoutPanelBarsFor)


# --- config: basis_panels validation --------------------------------------------------------------


def _cell() -> DiscoveryCellConfig:
    return DiscoveryCellConfig(
        market=_CRYPTO, window="w", symbol="BTCUSDT", venue=Venue.BINANCE, interval_seconds=86400
    )


def _panel(
    name: str, venue: Venue = Venue.BINANCE, market: AssetClass = _CRYPTO
) -> DiscoveryPanelConfig:
    return DiscoveryPanelConfig(
        name=name, market=market, venue=venue, interval_seconds=86400, symbols=["A", "B"]
    )


def test_basis_config_valid_pair_parses() -> None:
    cfg = DiscoveryConfig(
        cells=[_cell()],
        panels=[_panel("perps"), _panel("spot", venue=Venue.BINANCE_SPOT)],
        basis_panels=[
            DiscoveryBasisConfig(
                name="basis", market=_CRYPTO, perp_panel="perps", spot_panel="spot"
            )
        ],
    )
    assert cfg.basis_panels[0].spot_panel == "spot"


def test_basis_config_rejects_a_dangling_leg() -> None:
    with pytest.raises(ValidationError, match="not a configured panel"):
        DiscoveryConfig(
            cells=[_cell()],
            panels=[_panel("perps")],
            basis_panels=[
                DiscoveryBasisConfig(
                    name="basis", market=_CRYPTO, perp_panel="perps", spot_panel="missing"
                )
            ],
        )


def test_basis_config_rejects_a_name_collision_with_a_panel() -> None:
    with pytest.raises(ValidationError, match="collides with a panel name"):
        DiscoveryConfig(
            cells=[_cell()],
            panels=[_panel("perps"), _panel("spot", venue=Venue.BINANCE_SPOT)],
            basis_panels=[
                DiscoveryBasisConfig(
                    name="perps", market=_CRYPTO, perp_panel="perps", spot_panel="spot"
                )
            ],
        )


def test_basis_config_rejects_a_cross_market_leg() -> None:
    with pytest.raises(ValidationError, match="must share the cell's market"):
        DiscoveryConfig(
            cells=[_cell()],
            panels=[_panel("perps"), _panel("spot", market=AssetClass.EQUITY, venue=Venue.NSE)],
            basis_panels=[
                DiscoveryBasisConfig(
                    name="basis", market=_CRYPTO, perp_panel="perps", spot_panel="spot"
                )
            ],
        )


def test_basis_config_rejects_a_leg_hedging_itself() -> None:
    with pytest.raises(ValidationError, match="cannot hedge itself"):
        DiscoveryConfig(
            cells=[_cell()],
            panels=[_panel("perps")],
            basis_panels=[
                DiscoveryBasisConfig(
                    name="basis", market=_CRYPTO, perp_panel="perps", spot_panel="perps"
                )
            ],
        )


def test_basis_config_rejects_mismatched_leg_intervals() -> None:
    # a perp-1d x spot-1h pair would union daily+hourly stamps and silently fold all zeros —
    # the validator fails loud at load instead.
    hourly_spot = DiscoveryPanelConfig(
        name="spot",
        market=_CRYPTO,
        venue=Venue.BINANCE_SPOT,
        interval_seconds=3600,
        symbols=["A", "B"],
    )
    with pytest.raises(ValidationError, match="legs must share interval_seconds"):
        DiscoveryConfig(
            cells=[_cell()],
            panels=[_panel("perps"), hourly_spot],
            basis_panels=[
                DiscoveryBasisConfig(
                    name="basis", market=_CRYPTO, perp_panel="perps", spot_panel="spot"
                )
            ],
        )
