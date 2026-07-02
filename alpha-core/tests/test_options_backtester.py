"""The M5.5b chain-fold path — parity forward, condor selection, the fold's expiry-day
intrinsic settlement (THE trap test), costs at entry only, the options seal, and the TEST-3
wiring. Every P&L expectation is hand-computed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from alpha_core.core.enums import AssetClass, OptionRight, Venue
from alpha_core.data.options_store import OptionQuote, OptionsStore, seal_options_store
from alpha_core.execution.costs import CostModel
from alpha_core.helpers.config import (
    DiscoveryCellConfig,
    DiscoveryConfig,
    DiscoveryOptionsCellConfig,
    DiscoveryPanelConfig,
    load_yaml,
)
from alpha_core.research.options_backtester import (
    OPTIONS_TEMPLATES,
    HoldoutOptionsFor,
    OptionsChainBacktester,
    ResearchOptionsFor,
)
from alpha_core.research.promote import build_options_backtesters
from alpha_core.research.strategist import StrategyProposal
from alpha_core.strategy.examples.index_premium import (
    IronCondorConfig,
    IronCondorEod,
    parity_forward,
)

D = Decimal
_MKT = AssetClass.INDEX_OPTION
_CELL_NAME = "test-condor"
DAY0 = datetime(2024, 1, 1, tzinfo=UTC)


def _d(day: int) -> datetime:
    return DAY0 + timedelta(days=day)


def _q(
    day: int,
    expiry_day: int,
    strike: str,
    right: OptionRight,
    settle: str,
    oi: int = 1000,
) -> OptionQuote:
    return OptionQuote(
        underlying="NIFTY",
        venue=Venue.NSE,
        trade_date=_d(day),
        expiry=_d(expiry_day),
        strike=D(strike),
        right=right,
        open=D(0),
        high=D(0),
        low=D(0),
        close=D(settle),
        settle=D(settle),
        volume_contracts=1,
        open_interest=oi,
        change_in_oi=0,
    )


_CE = OptionRight.CALL
_PE = OptionRight.PUT

# A zeroed cost config: the math tests isolate the fold's marking arithmetic.
_ZERO_COSTS: dict[str, object] = {
    "slippage": {
        "index_option": {"type": "ticks", "value": 0},
        "default_spread": {"index_option_ticks": 0},
        "stress_multiplier": 1,
    },
    "segments": {"index_option": {}},
}

_CELL = DiscoveryOptionsCellConfig(
    name=_CELL_NAME,
    market=_MKT,
    venue=Venue.NSE,
    underlying="NIFTY",
    lot_size=65,
    tick_size=D("0.05"),
    min_oi=100,
)


def _bt(
    quotes: list[OptionQuote],
    *,
    cost_model: CostModel | None = None,
    min_bars: int = 1,
) -> OptionsChainBacktester:
    return OptionsChainBacktester(
        quotes_for=lambda _m, _w: quotes,
        cells={(_MKT, _CELL_NAME): _CELL},
        min_bars=min_bars,
        cost_model=cost_model if cost_model is not None else CostModel(_ZERO_COSTS),
    )


def _proposal(
    params: dict[str, int | Decimal] | None = None,
    template: str = "iron_condor_eod",
    window: str = _CELL_NAME,
) -> StrategyProposal:
    p: dict[str, int | Decimal] = params or {
        "target_dte": 7,
        "short_distance_pct": D("0.05"),
        "wing_width_pct": D("0.05"),
    }
    return StrategyProposal(template, p, _MKT, window, 1, "fp")


def _entry_chain(day: int, expiry_day: int) -> list[OptionQuote]:
    """A chain whose parity pair sits at 100 (CE=PE at 100 -> forward 100) with liquid strikes
    for shorts at 105/95 and wings at 110/90."""
    return [
        _q(day, expiry_day, "100", _CE, "3"),
        _q(day, expiry_day, "100", _PE, "3"),
        _q(day, expiry_day, "105", _CE, "2"),
        _q(day, expiry_day, "95", _PE, "2"),
        _q(day, expiry_day, "110", _CE, "1"),
        _q(day, expiry_day, "90", _PE, "1"),
    ]


# --- parity forward + selection --------------------------------------------------------------


def test_parity_forward_picks_min_gap_lower_strike_on_ties() -> None:
    chain = [
        _q(0, 7, "100", _CE, "3"),
        _q(0, 7, "100", _PE, "2"),  # gap 1
        _q(0, 7, "105", _CE, "2.5"),
        _q(0, 7, "105", _PE, "2.5"),  # gap 0 -> the forward
        _q(0, 7, "110", _CE, "4"),  # no put side -> not a pair
    ]
    assert parity_forward(chain) == D("105")
    assert parity_forward([_q(0, 7, "100", _CE, "3")]) is None  # no pair at all


def test_select_structure_snaps_to_liquid_strikes_and_respects_the_floor() -> None:
    strategy = IronCondorEod(
        IronCondorConfig(target_dte=7, short_distance_pct=D("0.05"), wing_width_pct=D("0.05"))
    )
    chain = _entry_chain(0, 7)
    legs = strategy.select_structure(chain, _d(7), D(100), min_oi=100)
    assert legs is not None
    assert [(str(x.strike), x.right, x.quantity) for x in legs] == [
        ("105", _CE, -1),
        ("95", _PE, -1),
        ("110", _CE, 1),
        ("90", _PE, 1),
    ]
    # raise the floor above every strike's OI -> nothing qualifies -> no structure.
    assert strategy.select_structure(chain, _d(7), D(100), min_oi=10_000) is None


def test_select_structure_refuses_a_crossed_or_degenerate_snap() -> None:
    # only strikes 100/105 exist: the wing snaps onto the short -> not strictly ordered -> None.
    strategy = IronCondorEod(
        IronCondorConfig(target_dte=7, short_distance_pct=D("0.03"), wing_width_pct=D("0.01"))
    )
    chain = [
        _q(0, 7, "100", _CE, "3"),
        _q(0, 7, "100", _PE, "3"),
        _q(0, 7, "105", _CE, "2"),
        _q(0, 7, "95", _PE, "2"),
    ]
    assert strategy.select_structure(chain, _d(7), D(100), min_oi=100) is None


def test_pick_expiry_nearest_target_within_the_admission_window() -> None:
    strategy = IronCondorEod(
        IronCondorConfig(target_dte=7, short_distance_pct=D("0.05"), wing_width_pct=D("0.05"))
    )
    assert strategy.pick_expiry({_d(2): 2, _d(8): 8, _d(30): 30}) == _d(8)  # 2 < min_dte 3
    assert strategy.pick_expiry({_d(2): 2, _d(50): 50}) is None  # nothing admissible


# --- the fold: hand-computed episode, THE expiry trap, costs ----------------------------------


def _episode_quotes() -> list[OptionQuote]:
    """Entry day 0 (DTE 7), a mark day 1, and expiry day 7 where every leg's row carries the
    FINAL-SETTLEMENT LEVEL 104 in `settle` (exactly what NSE publishes on expiry day)."""
    day1 = [
        _q(1, 7, "105", _CE, "1.5"),
        _q(1, 7, "95", _PE, "1.8"),
        _q(1, 7, "110", _CE, "0.8"),
        _q(1, 7, "90", _PE, "0.9"),
    ]
    expiry = [
        _q(7, 7, "105", _CE, "104"),
        _q(7, 7, "95", _PE, "104"),
        _q(7, 7, "110", _CE, "104"),
        _q(7, 7, "90", _PE, "104"),
    ]
    return _entry_chain(0, 7) + day1 + expiry


def test_fold_marks_daily_and_settles_at_intrinsic_not_settle() -> None:
    # THE trap test: on expiry day `settle` is the index level (104). Correct settlement is
    # intrinsic (all four legs OTM at 104 -> worth 0): pnl = +1.5 +1.8 -0.8 -0.9 = +1.6.
    # Marking 104 as a premium would instead produce a wildly wrong ~-206 day.
    returns = _bt(_episode_quotes()).run(_proposal())
    assert returns == [
        pytest.approx(0.0),  # entry day, zero-cost model: marks initialized, no move
        pytest.approx(0.004),  # (+0.5 +0.2 -0.2 -0.1) / forward 100
        pytest.approx(0.016),  # settlement: (+1.5 +1.8 -0.8 -0.9) / 100
    ]


def test_fold_settles_in_the_money_legs_at_intrinsic() -> None:
    # final level 107: short 105 CE worth 2 (a loss on the short), wings/put worthless.
    quotes = [
        *_entry_chain(0, 7),
        _q(7, 7, "105", _CE, "107"),
        _q(7, 7, "95", _PE, "107"),
        _q(7, 7, "110", _CE, "107"),
        _q(7, 7, "90", _PE, "107"),
    ]
    returns = _bt(quotes).run(_proposal())
    # short CE: -1*(2-2)=0? entry mark 2 -> settle mark 2 (intrinsic 107-105) -> 0 on that leg;
    # short PE +2, wing CE -1, wing PE -1 -> total 0.0 per unit.
    assert returns == [pytest.approx(0.0), pytest.approx(0.0)]


def test_costs_are_charged_at_entry_only() -> None:
    returns = _bt(_episode_quotes(), cost_model=CostModel(load_yaml("costs.yaml"))).run(_proposal())
    assert returns[0] < 0  # entry day pays the four-leg index_option stack
    assert returns[1] == pytest.approx(0.004)  # marking days carry no cost
    assert returns[2] == pytest.approx(0.016)  # cash settlement charges nothing extra


def test_no_reentry_on_the_settlement_day_but_next_day_is_eligible() -> None:
    # day 3 is BOTH the expiry and a perfect fresh entry opportunity (a later expiry's chain is
    # present and liquid); day 4 repeats that opportunity. One-structure-at-a-time means the
    # re-entry lands on day 4, so with REAL costs: day 3 = pure settlement, day 4 < 0 (entry).
    quotes = [
        *_entry_chain(0, 3),
        _q(3, 3, "105", _CE, "100"),
        _q(3, 3, "95", _PE, "100"),
        _q(3, 3, "110", _CE, "100"),
        _q(3, 3, "90", _PE, "100"),
        *_entry_chain(3, 10),  # the tempting same-day re-entry chain (DTE 7)
        *_entry_chain(4, 10),  # tomorrow's chain (DTE 6)
    ]
    proposal = _proposal(
        {"target_dte": 3, "short_distance_pct": D("0.05"), "wing_width_pct": D("0.05")}
    )
    returns = _bt(quotes, cost_model=CostModel(load_yaml("costs.yaml"))).run(proposal)
    assert len(returns) == 3
    # day 3: all legs expire worthless at 100 -> +2 +2 -1 -1 = +2 per unit, NO entry cost.
    assert returns[1] == pytest.approx(0.02)
    assert returns[2] < 0  # the re-entry (and only the re-entry) pays costs


def test_unknown_template_and_cell_raise_and_min_bars_guards() -> None:
    quotes = _entry_chain(0, 7)
    with pytest.raises(ValueError, match="unknown options template"):
        _bt(quotes).run(_proposal(template="iron_butterfly"))
    with pytest.raises(ValueError, match="no options cell mapped"):
        _bt(quotes).run(_proposal(window="not-a-cell"))
    with pytest.raises(ValueError, match="too few chain days"):
        _bt(quotes, min_bars=5).run(_proposal())


# --- template registry -------------------------------------------------------------------------


def test_template_space_is_the_preregistered_twelve_and_builds_from_decimals() -> None:
    template = OPTIONS_TEMPLATES["iron_condor_eod"]
    space = template.param_space
    sizes = []
    for spec in space.values():
        low, high, step = spec.low, spec.high, spec.step  # type: ignore[union-attr]
        sizes.append(int((high - low) / step) + 1)
    assert sorted(sizes) == [2, 2, 3] and 2 * 2 * 3 == 12
    strategy = template.build(
        {"target_dte": D("28"), "short_distance_pct": D("0.04"), "wing_width_pct": D("0.02")}
    )
    assert isinstance(strategy, IronCondorEod)
    assert strategy.config.target_dte == 28  # Decimal grid value coerces to the int field


# --- the options seal + TEST-3 wiring ------------------------------------------------------------


def _span_quotes(n_days: int) -> list[OptionQuote]:
    return [_q(day, day + 7, "100", _CE, "1") for day in range(n_days)]


def test_seal_options_store_splits_and_writes_a_fresh_boundary(tmp_path: Path) -> None:
    source = OptionsStore(tmp_path / "raw")
    research = OptionsStore(tmp_path / "research")
    holdout = OptionsStore(tmp_path / "holdout")
    source.write(_span_quotes(10))  # days 0..9 -> fraction 0.2 locks the tail
    windows = seal_options_store(source, research=research, holdout=holdout, fraction=0.2)
    (window,) = windows.values()
    r = research.read(underlying="NIFTY", venue=Venue.NSE)
    h = holdout.read(underlying="NIFTY", venue=Venue.NSE)
    assert r and h and len(r) + len(h) == 10
    assert all(q.trade_date < window.start for q in r)
    assert all(q.trade_date >= window.start for q in h)
    assert (holdout.root / "_windows.json").exists()


def test_seal_options_store_floor_ratchets_on_backward_extension(tmp_path: Path) -> None:
    source = OptionsStore(tmp_path / "raw")
    research = OptionsStore(tmp_path / "research")
    holdout = OptionsStore(tmp_path / "holdout")
    source.write(_span_quotes(10))
    first = seal_options_store(source, research=research, holdout=holdout, fraction=0.2)
    (first_window,) = first.values()
    # extend history BACKWARD (a longer span would drag a fresh boundary earlier) -> the floor
    # pins the boundary: researched days can never become "holdout".
    early = [
        OptionQuote(
            underlying="NIFTY",
            venue=Venue.NSE,
            trade_date=datetime(2023, 1, 1, tzinfo=UTC) + timedelta(days=i),
            expiry=datetime(2023, 1, 8, tzinfo=UTC) + timedelta(days=i),
            strike=D("100"),
            right=_CE,
            open=D(0),
            high=D(0),
            low=D(0),
            close=D("1"),
            settle=D("1"),
            volume_contracts=1,
            open_interest=1,
            change_in_oi=0,
        )
        for i in range(300)
    ]
    source.write(early)
    second = seal_options_store(source, research=research, holdout=holdout, fraction=0.2)
    (second_window,) = second.values()
    assert second_window.start == first_window.start  # ratcheted, never dragged backward


def test_seal_options_store_rejects_overlapping_roots(tmp_path: Path) -> None:
    store = OptionsStore(tmp_path / "raw")
    with pytest.raises(ValueError, match="disjoint"):
        seal_options_store(
            store,
            research=OptionsStore(tmp_path / "raw" / "nested"),
            holdout=OptionsStore(tmp_path / "holdout"),
            fraction=0.2,
        )


def test_build_options_backtesters_wires_the_test3_boundary(tmp_path: Path) -> None:
    in_sample, holdout_bt = build_options_backtesters(
        options_research=OptionsStore(tmp_path / "research"),
        options_holdout=OptionsStore(tmp_path / "holdout"),
    )
    assert isinstance(in_sample, OptionsChainBacktester)
    assert isinstance(holdout_bt, OptionsChainBacktester)
    assert isinstance(in_sample._quotes_for, ResearchOptionsFor)
    assert not isinstance(in_sample._quotes_for, HoldoutOptionsFor)
    assert isinstance(holdout_bt._quotes_for, HoldoutOptionsFor)


def test_research_options_for_reads_the_configured_cell(tmp_path: Path) -> None:
    store = OptionsStore(tmp_path)
    store.write(_span_quotes(3))
    reader = ResearchOptionsFor.from_config(store)  # the real discovery.yaml cells
    assert len(reader(_MKT, "nifty-condor-eod")) == 3
    with pytest.raises(ValueError, match="no options cell mapped"):
        reader(_MKT, "not-a-cell")


# --- config validation ----------------------------------------------------------------------


def test_options_cell_name_collision_with_a_panel_is_rejected() -> None:
    cell = DiscoveryCellConfig(
        market=AssetClass.CRYPTO,
        window="w",
        symbol="BTCUSDT",
        venue=Venue.BINANCE,
        interval_seconds=86400,
    )
    panel = DiscoveryPanelConfig(
        name="clash",
        market=AssetClass.CRYPTO,
        venue=Venue.BINANCE,
        interval_seconds=86400,
        symbols=["A", "B"],
    )
    options_cell = DiscoveryOptionsCellConfig(
        name="clash",
        market=_MKT,
        venue=Venue.NSE,
        underlying="NIFTY",
        lot_size=65,
        tick_size=D("0.05"),
        min_oi=0,
    )
    with pytest.raises(ValidationError, match="collides with a panel/basis name"):
        DiscoveryConfig(cells=[cell], panels=[panel], options_cells=[options_cell])
