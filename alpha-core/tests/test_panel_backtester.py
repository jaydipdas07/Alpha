"""The cross-sectional panel path (M3.0) — the strategy logic, the panel backtester, the cold-store
panel boundary, and the end-to-end panel discovery cycle (strategist injection -> backtest ->
adjudicate). The crown-jewel checks: reversal returns *negate* momentum returns (so the whole
score -> rank -> weight -> portfolio-return pipeline is correct), and perturbing a future bar leaves
earlier returns untouched (look-ahead-clean, TEST-1)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.funding import FundingRate, FundingStore
from alpha_core.data.holdout import HoldoutStore, HoldoutWindow
from alpha_core.data.store import BarStore
from alpha_core.research.cold_store_bars import SeriesCoord
from alpha_core.research.discovery import run_discovery_cycle
from alpha_core.research.funding_backtester import ColdStoreFundingFor
from alpha_core.research.holdout_gate import HoldoutPanelBarsFor
from alpha_core.research.panel_backtester import (
    PANEL_TEMPLATES,
    ColdStorePanelBarsFor,
    PanelBacktester,
    align_closes,
    simulate_panel,
    turnover_cost_fraction,
)
from alpha_core.research.promote import build_panel_backtesters
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.quant_analyst import QuantAnalyst
from alpha_core.research.strategist import (
    RandomProposer,
    Strategist,
    StrategistError,
    StrategyProposal,
)
from alpha_core.strategy.examples.cross_sectional import (
    CrossSectionalConfig,
    CrossSectionalMomentum,
    CrossSectionalReversal,
)

DAY = timedelta(days=1)
START = datetime(2024, 1, 1, tzinfo=UTC)
_CRYPTO = AssetClass.CRYPTO


def _closes(d: dict[str, list[str]]) -> dict[str, list[Decimal]]:
    return {k: [Decimal(x) for x in v] for k, v in d.items()}


def _bars(symbol: str, closes: list[str], offset: int = 0) -> list[Bar]:
    """A flat OHLCV series (open=high=low=close) at consecutive daily UTC starts — the cross-section
    varies *across* bars (the trailing-return signal), not within one. ``offset`` shifts the first
    bar by whole days (a later listing)."""
    out: list[Bar] = []
    for i, c in enumerate(closes):
        price = Decimal(c)
        out.append(
            Bar(
                symbol=symbol,
                venue=Venue.BINANCE,
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


def _panel(closes_by_symbol: dict[str, list[str]]) -> dict[str, list[Bar]]:
    return {sym: _bars(sym, closes) for sym, closes in closes_by_symbol.items()}


def _proposal(template: str, params: dict[str, int]) -> StrategyProposal:
    return StrategyProposal(template, params, _CRYPTO, "crypto-perps-1d", 1, "fp")


def _bt(
    panel: dict[str, list[Bar]], *, cost: Decimal = Decimal(0), min_bars: int = 1
) -> PanelBacktester:
    return PanelBacktester(
        panel_bars_for=lambda _market, _window: panel, min_bars=min_bars, cost_fraction=cost
    )


# --- the cross-sectional strategy: scoring, ranking, dollar-neutral weights ----------------------


def test_momentum_longs_winners_shorts_losers() -> None:
    # returns over lookback=1: A +100%, B 0, C -50%, D 0 -> long the top (A), short the bottom (C).
    closes = _closes({"A": ["1", "2"], "B": ["2", "2"], "C": ["2", "1"], "D": ["1", "1"]})
    weights = CrossSectionalMomentum(CrossSectionalConfig(lookback=1, top_k=1)).target_weights(
        closes
    )
    assert weights == {"A": Decimal(1), "C": Decimal(-1)}


def test_reversal_is_momentum_with_flipped_sign() -> None:
    closes = _closes({"A": ["1", "2"], "B": ["2", "2"], "C": ["2", "1"], "D": ["1", "1"]})
    cfg = CrossSectionalConfig(lookback=1, top_k=1)
    momentum = CrossSectionalMomentum(cfg).target_weights(closes)
    reversal = CrossSectionalReversal(cfg).target_weights(closes)
    assert reversal == {sym: -w for sym, w in momentum.items()}


def test_weights_are_dollar_neutral_and_equal_weight() -> None:
    # returns: A +300%, B +200%, C +100%, D 0, E -50% -> long top2 (A,B), short bottom2 (D,E).
    closes = _closes(
        {"A": ["1", "4"], "B": ["1", "3"], "C": ["1", "2"], "D": ["2", "2"], "E": ["2", "1"]}
    )
    weights = CrossSectionalMomentum(CrossSectionalConfig(lookback=1, top_k=2)).target_weights(
        closes
    )
    assert sum(weights.values()) == Decimal(0)  # dollar-neutral
    assert sum(abs(w) for w in weights.values()) == Decimal(2)  # gross 2 (each leg fully invested)
    half = Decimal(1) / Decimal(2)
    assert weights == {"A": half, "B": half, "D": -half, "E": -half}  # C (the middle) is flat


def test_too_few_scored_names_takes_no_book() -> None:
    # only 2 names but top_k=2 needs 2*2=4 -> no book that bar.
    closes = _closes({"A": ["1", "2", "3"], "B": ["3", "2", "1"]})
    assert (
        CrossSectionalMomentum(CrossSectionalConfig(lookback=1, top_k=2)).target_weights(closes)
        == {}
    )


def test_name_without_enough_history_is_excluded() -> None:
    # C has only one close (< lookback+1) -> it has no score and cannot be selected.
    closes = _closes({"A": ["1", "2", "3"], "B": ["1", "2", "3"], "C": ["5"], "D": ["3", "2", "1"]})
    weights = CrossSectionalMomentum(CrossSectionalConfig(lookback=1, top_k=1)).target_weights(
        closes
    )
    assert "C" not in weights


def test_nonpositive_base_price_yields_no_score() -> None:
    # a defensive guard against degenerate data: a non-positive base price -> no trailing return.
    closes = _closes({"A": ["0", "2"], "B": ["1", "2"], "C": ["2", "1"], "D": ["1", "1"]})
    weights = CrossSectionalMomentum(CrossSectionalConfig(lookback=1, top_k=1)).target_weights(
        closes
    )
    assert "A" not in weights  # base price 0 -> A is unscored and cannot be selected


def test_config_rejects_nonpositive_and_extra() -> None:
    with pytest.raises(ValidationError):
        CrossSectionalConfig(lookback=0, top_k=1)
    with pytest.raises(ValidationError):
        CrossSectionalConfig(lookback=5, top_k=1, bogus=1)  # type: ignore[call-arg]


# --- the panel backtester: returns series, the negation invariant, costs, look-ahead -------------

# a panel with strictly distinct per-bar returns (A x2 > C x1.5 > D x1.1 > B x0.5), so momentum and
# reversal pick exactly opposite names (no ties) at every rebalance.
_DISTINCT = {
    "A": ["1", "2", "4", "8", "16", "32"],
    "B": ["32", "16", "8", "4", "2", "1"],
    "C": ["1", "1.5", "2.25", "3.375", "5.0625", "7.59375"],
    "D": ["10", "11", "12.1", "13.31", "14.641", "16.1051"],
}
_PARAMS = {"lookback": 1, "top_k": 1, "holding_period": 1}


def test_returns_series_length_is_bars_minus_lookback_minus_one() -> None:
    returns = _bt(_panel(_DISTINCT)).run(_proposal("cross_sectional_momentum", _PARAMS))
    assert len(returns) == 6 - 1 - 1  # n - 1 - lookback


def test_reversal_returns_negate_momentum_returns() -> None:
    # whole-pipeline check: reversal holds -momentum's book, so (cost-free) the returns negate.
    panel = _panel(_DISTINCT)
    momentum = list(_bt(panel).run(_proposal("cross_sectional_momentum", _PARAMS)))
    reversal = list(_bt(panel).run(_proposal("cross_sectional_reversal", _PARAMS)))
    assert reversal == pytest.approx([-r for r in momentum])


def test_turnover_cost_never_increases_a_return() -> None:
    panel = _panel(_DISTINCT)
    gross = list(_bt(panel, cost=Decimal(0)).run(_proposal("cross_sectional_momentum", _PARAMS)))
    net = list(_bt(panel, cost=Decimal("0.01")).run(_proposal("cross_sectional_momentum", _PARAMS)))
    assert all(n <= g for n, g in zip(net, gross, strict=True))
    assert net[0] < gross[0]  # the first rebalance builds the book from flat -> turnover -> cost


def test_no_lookahead_a_future_bar_leaves_earlier_returns_unchanged() -> None:
    params = {"lookback": 1, "top_k": 1, "holding_period": 1}
    base = list(_bt(_panel(_DISTINCT)).run(_proposal("cross_sectional_momentum", params)))
    bumped_closes = {**_DISTINCT, "A": [*_DISTINCT["A"][:-1], "999"]}  # only the last bar
    bumped = list(_bt(_panel(bumped_closes)).run(_proposal("cross_sectional_momentum", params)))
    assert base[:-1] == bumped[:-1]  # only the last return (which uses the last bar) may move


class _SpyStrategy:
    """Records each rebalance: the current bar's close (identifies WHICH bar, since the fold hands
    the trailing window ending at the rebalance bar) and the window width it was handed."""

    def __init__(self, config: CrossSectionalConfig, calls: list[tuple[Decimal, int]]) -> None:
        self._config = config
        self._calls = calls

    @property
    def config(self) -> CrossSectionalConfig:
        return self._config

    def target_weights(self, closes: Mapping[str, Sequence[Decimal]]) -> dict[str, Decimal]:
        series = next(iter(closes.values()))
        self._calls.append((series[-1], len(series)))
        return {}


def test_holding_period_controls_the_rebalance_cadence() -> None:
    # simulate_panel re-ranks only every `holding_period` bars (between, it holds the prior book by
    # construction — `held` is reassigned only inside the cadence branch), and each rebalance hands
    # the strategy exactly the trailing lookback+1 window ending at the rebalance bar.
    closes = {sym: [Decimal(i) for i in range(12)] for sym in ("A", "B", "C", "D")}
    calls: list[tuple[Decimal, int]] = []
    cfg = CrossSectionalConfig(lookback=2, top_k=1, holding_period=3)
    simulate_panel(_SpyStrategy(cfg, calls), closes, closes, {}, n=12, cost=Decimal(0))
    # warmup=2, then every 3 bars over range(2, 11): bars 2, 5, 8 (close == the bar index).
    assert calls == [(Decimal(2), 3), (Decimal(5), 3), (Decimal(8), 3)]


class _FixedBook:
    """A strategy that always holds the same book — to drive a held symbol onto a gap close."""

    config = CrossSectionalConfig(lookback=1, top_k=1)

    def target_weights(self, closes: Mapping[str, Sequence[Decimal]]) -> dict[str, Decimal]:
        return {"A": Decimal(1), "B": Decimal(-1)}


def test_nonpositive_close_for_a_held_symbol_does_not_crash() -> None:
    # A's close is 0 at bar index 2 (a data gap); A is held, so without the guard the i=2 forward
    # return divides by it -> DivisionByZero. The guard skips it (flat), so the run completes.
    closes = {
        "A": [Decimal(x) for x in ("1", "2", "0", "4", "5")],
        "B": [Decimal(1)] * 5,
    }
    returns = simulate_panel(_FixedBook(), closes, closes, {}, n=5, cost=Decimal(0))
    assert len(returns) == 5 - 1 - 1
    assert all(isinstance(r, float) for r in returns)


def test_too_few_aligned_bars_raises() -> None:
    panel = _panel(
        {"A": ["1", "2", "3"], "B": ["3", "2", "1"], "C": ["1", "1", "1"], "D": ["2", "2", "2"]}
    )
    bt = PanelBacktester(
        panel_bars_for=lambda _m, _w: panel, cost_fraction=Decimal(0)
    )  # default min_bars
    with pytest.raises(ValueError, match="too few aligned"):
        bt.run(_proposal("cross_sectional_momentum", _PARAMS))


def test_panel_with_no_ingested_bars_raises_too_few() -> None:
    # every member empty (un-ingested) -> empty alignment -> the rigor-gate guard fires.
    bt = PanelBacktester(panel_bars_for=lambda _m, _w: {"A": [], "B": []}, cost_fraction=Decimal(0))
    with pytest.raises(ValueError, match="too few aligned"):
        bt.run(_proposal("cross_sectional_momentum", _PARAMS))


def test_unknown_panel_template_raises() -> None:
    bt = PanelBacktester(panel_bars_for=lambda _m, _w: {}, min_bars=1)
    with pytest.raises(ValueError, match="unknown panel template"):
        bt.run(_proposal("ma_crossover", {"fast_period": 1}))


def test_turnover_cost_fraction_crypto_and_equity() -> None:
    # crypto panels are PERP universes: 3 bps perp slippage + 0.0005 perp taker (crypto_perp keys —
    # NOT the spot fee + TDS regime the old single `crypto` segment overcharged, ~2.5x).
    assert turnover_cost_fraction(_CRYPTO) == Decimal("0.0008")
    with pytest.raises(NotImplementedError):
        turnover_cost_fraction(AssetClass.EQUITY)


# --- the unbalanced panel: union timeline + point-in-time membership -----------------------------


def test_align_closes_unions_the_timeline_and_marks_missing_slots() -> None:
    # a later listing does NOT truncate the panel (the old inner-join would drop bar 0 entirely);
    # its missing slots are None — no forward-fill, membership is the fold's job.
    panel = {"A": _bars("A", ["1", "2", "3"]), "E": _bars("E", ["5", "6"], offset=1)}
    timeline, closes = align_closes(panel)
    assert timeline == [START + i * DAY for i in range(3)]
    assert closes["A"] == [Decimal(1), Decimal(2), Decimal(3)]
    assert closes["E"] == [None, Decimal(5), Decimal(6)]


def test_late_listed_member_joins_only_once_its_trailing_window_is_real() -> None:
    # E lists at bar 2 of 6 and, once scoreable, is the runaway winner (x3 vs A's x2). Before its
    # lookback+1 window is real the book must be IDENTICAL to the E-less panel (no pre-listing
    # membership, no fill); after, momentum longs E — point-in-time membership end to end.
    base = _panel(_DISTINCT)
    with_e = {**_panel(_DISTINCT), "E": _bars("E", ["1", "3", "9", "27"], offset=2)}
    params = {"lookback": 1, "top_k": 1, "holding_period": 1}
    base_returns = list(_bt(base).run(_proposal("cross_sectional_momentum", params)))
    e_returns = list(_bt(with_e).run(_proposal("cross_sectional_momentum", params)))
    assert len(e_returns) == len(base_returns)  # the union timeline is the full 6 bars
    assert e_returns[:2] == base_returns[:2]  # E not yet scoreable -> books identical
    assert e_returns[2] != base_returns[2]  # E's window is real from bar 3 -> it takes the long leg
    assert e_returns[2] == pytest.approx(2.0 - (-0.5))  # long E (x3), short B (x0.5)


def test_member_that_stops_printing_earns_a_flat_zero() -> None:
    # a held symbol whose close disappears (a gap / delisting) contributes 0 from the missing bar
    # on — never a fabricated return off a filled price.
    closes: dict[str, list[Decimal | None]] = {
        "A": [Decimal(1), Decimal(2), Decimal(4), None, None],
        "B": [Decimal(1)] * 5,
    }
    returns = simulate_panel(_FixedBook(), closes, closes, {}, n=5, cost=Decimal(0))
    assert returns == pytest.approx([1.0, 0.0, 0.0])  # A doubles once, then its data stops


# --- funding folded into the PRICE panel (a perp book pays/earns funding continuously) -----------


def _funding_rates(symbol: str, rates: list[str]) -> list[FundingRate]:
    """One funding payment per UTC day at midnight (so daily_funding keys match the bar starts)."""
    return [
        FundingRate(
            symbol=symbol, venue=Venue.BINANCE, funding_time=START + i * DAY, rate=Decimal(r)
        )
        for i, r in enumerate(rates)
    ]


def _funding_bt(
    panel: dict[str, list[Bar]], funding: dict[str, list[FundingRate]]
) -> PanelBacktester:
    return PanelBacktester(
        panel_bars_for=lambda _m, _w: panel,
        panel_funding_for=lambda _m, _w: funding,
        min_bars=1,
        cost_fraction=Decimal(0),
    )


def test_flat_price_panel_with_funding_returns_the_pure_book_carry() -> None:
    # flat prices -> all trailing returns tie at 0 -> the symbol tiebreak longs A, shorts D. The
    # price P&L is 0, so each bar's return is exactly the held book's funding transfer
    # (-w·funding): the long (A, +0.001) PAYS, the short (D, -0.001) PAYS -> -0.002/bar — the same
    # fold arithmetic the carry backtester pins (fold parity with #142).
    n = 6
    panel = _panel({s: ["100"] * n for s in ("A", "B", "C", "D")})
    funding = {
        "A": _funding_rates("A", ["0.001"] * n),
        "B": _funding_rates("B", ["0"] * n),
        "C": _funding_rates("C", ["0"] * n),
        "D": _funding_rates("D", ["-0.001"] * n),
    }
    returns = list(_funding_bt(panel, funding).run(_proposal("cross_sectional_momentum", _PARAMS)))
    assert returns and all(r == pytest.approx(-0.002) for r in returns)


def test_positive_funding_bleeds_a_momentum_book() -> None:
    # momentum longs the winner (A); with positive funding on A the funding-aware fold must earn
    # LESS than the funding-blind one — the transfer a perp momentum book actually pays.
    panel = _panel(_DISTINCT)
    n = len(_DISTINCT["A"])
    funding = {
        "A": _funding_rates("A", ["0.001"] * n),  # the winner momentum longs -> pays funding
        "B": _funding_rates("B", ["0"] * n),
        "C": _funding_rates("C", ["0"] * n),
        "D": _funding_rates("D", ["0"] * n),
    }
    blind = list(_bt(panel).run(_proposal("cross_sectional_momentum", _PARAMS)))
    aware = list(_funding_bt(panel, funding).run(_proposal("cross_sectional_momentum", _PARAMS)))
    assert all(a < b for a, b in zip(aware, blind, strict=True))


def test_member_without_funding_history_transfers_nothing() -> None:
    # a member absent from the funding source contributes 0 carry (nothing known to transfer) —
    # the book still runs (the price leg is unaffected).
    n = 6
    panel = _panel({s: ["100"] * n for s in ("A", "B", "C", "D")})
    funding = {"D": _funding_rates("D", ["-0.001"] * n)}  # only the short (D) has funding
    returns = list(_funding_bt(panel, funding).run(_proposal("cross_sectional_momentum", _PARAMS)))
    assert returns and all(r == pytest.approx(-0.001) for r in returns)  # only D's leg transfers


def test_price_panel_with_funding_requires_a_daily_panel() -> None:
    # the same fail-loud guard as the carry backtester (funding aggregates to UTC days).
    hour = timedelta(hours=1)
    bars = {
        s: [
            Bar(
                symbol=s,
                venue=Venue.BINANCE,
                asset_class=_CRYPTO,
                start=START + i * hour,
                interval=hour,
                open=Decimal(100),
                high=Decimal(100),
                low=Decimal(100),
                close=Decimal(100),
                volume=Decimal(1),
            )
            for i in range(30)
        ]
        for s in ("A", "B", "C", "D")
    }
    funding = {s: _funding_rates(s, ["0.0001"] * 30) for s in ("A", "B", "C", "D")}
    with pytest.raises(NotImplementedError, match="daily panel"):
        _funding_bt(bars, funding).run(_proposal("cross_sectional_momentum", _PARAMS))


# --- the cold-store panel boundary ---------------------------------------------------------------


def test_cold_store_panel_reads_only_ingested_members(tmp_path: object) -> None:
    store = BarStore(tmp_path)  # type: ignore[arg-type]
    store.write_bars(_bars("BTCUSDT", ["1", "2", "3"]))
    store.write_bars(_bars("ETHUSDT", ["3", "2", "1"]))
    members = ColdStorePanelBarsFor.from_config(store)(_CRYPTO, "crypto-perps-1d")
    assert set(members) == {"BTCUSDT", "ETHUSDT"}  # the other 40 members read empty -> dropped


def test_cold_store_panel_unmapped_raises(tmp_path: object) -> None:
    source = ColdStorePanelBarsFor(BarStore(tmp_path), {})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no discovery panel mapped"):
        source(_CRYPTO, "not-a-panel")


def test_cold_store_panel_rejects_asset_class_mismatch(tmp_path: object) -> None:
    store = BarStore(tmp_path)  # type: ignore[arg-type]
    store.write_bars(_bars("BTCUSDT", ["1", "2", "3"]))  # BTCUSDT bars are CRYPTO
    panels = {(AssetClass.EQUITY, "p"): [SeriesCoord("BTCUSDT", Venue.BINANCE, 86400)]}
    with pytest.raises(ValueError, match="not EQUITY"):
        ColdStorePanelBarsFor(store, panels)(AssetClass.EQUITY, "p")  # claims EQUITY -> fail fast


def test_build_panel_backtesters_wires_the_test3_boundary(tmp_path: Path) -> None:
    # the load-bearing TEST-3 wiring: in-sample reads the cold/research store, holdout the gate-only
    # store. A future edit that swapped the two would flip these reader types and fail here.
    in_sample, holdout_bt = build_panel_backtesters(
        research_store=BarStore(tmp_path / "research"),
        holdout_store=HoldoutStore(tmp_path / "holdout"),
    )
    assert isinstance(in_sample, PanelBacktester) and isinstance(holdout_bt, PanelBacktester)
    assert isinstance(in_sample._panel_bars_for, ColdStorePanelBarsFor)
    assert isinstance(holdout_bt._panel_bars_for, HoldoutPanelBarsFor)
    assert in_sample._panel_funding_for is None and holdout_bt._panel_funding_for is None


def test_build_panel_backtesters_wires_funding_into_both_folds(tmp_path: Path) -> None:
    # a perp panel's funding source reaches BOTH folds (each fold's price timeline gates which
    # funding days it can touch, so one raw store serves both sides).
    in_sample, holdout_bt = build_panel_backtesters(
        research_store=BarStore(tmp_path / "research"),
        holdout_store=HoldoutStore(tmp_path / "holdout"),
        funding_store=FundingStore(tmp_path / "funding"),
    )
    assert isinstance(in_sample, PanelBacktester) and isinstance(holdout_bt, PanelBacktester)
    assert isinstance(in_sample._panel_funding_for, ColdStoreFundingFor)
    assert isinstance(holdout_bt._panel_funding_for, ColdStoreFundingFor)


# --- the gate-only holdout panel boundary (TEST-3 — the single legitimate read) ------------------


def _holdout(tmp_path: object, *series: list[Bar]) -> HoldoutStore:
    store = HoldoutStore(tmp_path)  # type: ignore[arg-type]
    bars = [bar for s in series for bar in s]
    store.replace(bars, HoldoutWindow(start=START, end=START + 9 * DAY, version="v"))
    return store


def test_holdout_panel_reads_only_ingested_members(tmp_path: object) -> None:
    store = _holdout(tmp_path, _bars("BTCUSDT", ["1", "2", "3"]), _bars("ETHUSDT", ["3", "2", "1"]))
    members = HoldoutPanelBarsFor.from_config(store)(_CRYPTO, "crypto-perps-1d")
    assert set(members) == {"BTCUSDT", "ETHUSDT"}  # the other 40 members read empty -> dropped


def test_holdout_panel_unmapped_raises(tmp_path: object) -> None:
    source = HoldoutPanelBarsFor(HoldoutStore(tmp_path), {})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no discovery panel mapped"):
        source(_CRYPTO, "not-a-panel")


def test_holdout_panel_rejects_asset_class_mismatch(tmp_path: object) -> None:
    store = _holdout(tmp_path, _bars("BTCUSDT", ["1", "2", "3"]))  # BTCUSDT bars are CRYPTO
    panels = {(AssetClass.EQUITY, "p"): [SeriesCoord("BTCUSDT", Venue.BINANCE, 86400)]}
    with pytest.raises(ValueError, match="not EQUITY"):
        HoldoutPanelBarsFor(store, panels)(AssetClass.EQUITY, "p")  # claims EQUITY -> fail fast


# --- the strategist drives the injected panel registry, and the cycle runs end-to-end ------------


def test_strategist_proposes_from_the_injected_panel_registry() -> None:
    with ProposalLedger() as ledger:
        strategist = Strategist(ledger, proposer=RandomProposer(seed=1), templates=PANEL_TEMPLATES)
        proposal = strategist.propose(
            "cross_sectional_momentum", market=_CRYPTO, window="crypto-perps-1d"
        )
    assert proposal.template == "cross_sectional_momentum"
    assert set(proposal.params) == {"lookback", "top_k", "holding_period"}


def test_strategist_unknown_template_lists_the_injected_registry() -> None:
    with ProposalLedger() as ledger:
        strategist = Strategist(ledger, templates=PANEL_TEMPLATES)
        # ma_crossover is a single-instrument template, absent from the panel registry.
        with pytest.raises(StrategistError, match="cross_sectional_momentum"):
            strategist.propose("ma_crossover", market=_CRYPTO, window="crypto-perps-1d")


def test_panel_discovery_cycle_runs_end_to_end() -> None:
    # a 70-bar panel with a stable ranking (A steepest up, B down) -> the panel path runs end to end
    # (propose -> panel-backtest -> adjudicate), recording one trial per candidate (the wiring).
    panel = _panel(
        {
            "A": [str(100 + 3 * i) for i in range(70)],
            "B": [str(250 - i) for i in range(70)],
            "C": [str(100 + i) for i in range(70)],
            "D": [str(100 + 2 * i) for i in range(70)],
        }
    )
    backtester = _bt(panel, cost=Decimal(0), min_bars=2)
    with ProposalLedger() as ledger:
        report = run_discovery_cycle(
            "cross_sectional_momentum",
            market=_CRYPTO,
            window="crypto-perps-1d",
            strategist=Strategist(
                ledger, proposer=RandomProposer(seed=3), templates=PANEL_TEMPLATES
            ),
            quant_analyst=QuantAnalyst(),
            backtester=backtester,
            n_candidates=3,
        )
        assert ledger.count(_CRYPTO, "cross_sectional_momentum", "crypto-perps-1d") == 3
    assert len(report.assessments) == 3
