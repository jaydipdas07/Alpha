"""The cross-sectional panel path (M3.0) — the strategy logic, the panel backtester, the cold-store
panel boundary, and the end-to-end panel discovery cycle (strategist injection -> backtest ->
adjudicate). The crown-jewel checks: reversal returns *negate* momentum returns (so the whole
score -> rank -> weight -> portfolio-return pipeline is correct), and perturbing a future bar leaves
earlier returns untouched (look-ahead-clean, TEST-1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.store import BarStore
from alpha_core.research.discovery import run_discovery_cycle
from alpha_core.research.panel_backtester import (
    PANEL_TEMPLATES,
    ColdStorePanelBarsFor,
    PanelBacktester,
    turnover_cost_fraction,
)
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


def _bars(symbol: str, closes: list[str]) -> list[Bar]:
    """A flat OHLCV series (open=high=low=close) at consecutive daily UTC starts — the cross-section
    varies *across* bars (the trailing-return signal), not within one."""
    out: list[Bar] = []
    for i, c in enumerate(closes):
        price = Decimal(c)
        out.append(
            Bar(
                symbol=symbol,
                venue=Venue.BINANCE,
                asset_class=_CRYPTO,
                start=START + i * DAY,
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
    assert turnover_cost_fraction(_CRYPTO) == Decimal("0.0018")  # 8 bps slippage + 0.001 taker fee
    with pytest.raises(NotImplementedError):
        turnover_cost_fraction(AssetClass.EQUITY)


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
