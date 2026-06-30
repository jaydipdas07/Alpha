"""The funding-carry path (M3.0): the FundingCarry strategy + the FundingPanelBacktester.

Crown-jewel checks: carry **shorts** the highest-funding perps and **longs** the lowest (the harvest
direction); the flat-price return is the **pure carry** (sum of -w*funding); and perturbing a future
bar leaves earlier returns untouched (look-ahead-clean, TEST-1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.funding import FundingRate, FundingStore
from alpha_core.research.funding_backtester import (
    FUNDING_TEMPLATES,
    ColdStoreFundingFor,
    FundingPanelBacktester,
)
from alpha_core.research.strategist import StrategyProposal
from alpha_core.strategy.examples.cross_sectional import CrossSectionalConfig, FundingCarry

DAY = timedelta(days=1)
START = datetime(2024, 1, 1, tzinfo=UTC)  # a UTC midnight, so funding aligns to the daily bar start
_CRYPTO = AssetClass.CRYPTO
_PARAMS = {"lookback": 1, "top_k": 1, "holding_period": 1}


def _seq(d: dict[str, list[str]]) -> dict[str, list[Decimal]]:
    return {k: [Decimal(x) for x in v] for k, v in d.items()}


def _bar(symbol: str, i: int, price: Decimal, interval: timedelta) -> Bar:
    return Bar(
        symbol=symbol,
        venue=Venue.BINANCE,
        asset_class=_CRYPTO,
        start=START + i * interval,
        interval=interval,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(1),
    )


def _flat_bars(symbol: str, n: int, price: str = "100", interval: timedelta = DAY) -> list[Bar]:
    return [_bar(symbol, i, Decimal(price), interval) for i in range(n)]


def _price_bars(symbol: str, prices: list[str]) -> list[Bar]:
    """Daily bars whose close follows ``prices`` (so the book earns a real price return)."""
    return [_bar(symbol, i, Decimal(px), DAY) for i, px in enumerate(prices)]


def _funding(symbol: str, rates: list[str]) -> list[FundingRate]:
    """One funding payment per UTC day at midnight (so daily_funding keys match the bar starts)."""
    return [
        FundingRate(
            symbol=symbol, venue=Venue.BINANCE, funding_time=START + i * DAY, rate=Decimal(r)
        )
        for i, r in enumerate(rates)
    ]


def _proposal(params: dict[str, int]) -> StrategyProposal:
    return StrategyProposal("cross_sectional_carry", params, _CRYPTO, "crypto-perps-1d", 1, "fp")


def _bt(
    prices: dict[str, list[Bar]],
    funding: dict[str, list[FundingRate]],
    *,
    cost: Decimal = Decimal(0),
    min_bars: int = 1,
) -> FundingPanelBacktester:
    return FundingPanelBacktester(
        panel_bars_for=lambda _m, _w: prices,
        panel_funding_for=lambda _m, _w: funding,
        min_bars=min_bars,
        cost_fraction=cost,
    )


# --- the carry strategy: rank by -funding (short high, long low) ---------------------------------


def test_carry_longs_lowest_funding_shorts_highest() -> None:
    # trailing-mean funding (lookback=1) = last value. A high, B negative, C 0, D mid-positive.
    funding = _seq({"A": ["0.001"], "B": ["-0.001"], "C": ["0"], "D": ["0.0005"]})
    weights = FundingCarry(CrossSectionalConfig(lookback=1, top_k=1)).target_weights(funding)
    # carry longs the most-negative (B, paid to hold) and shorts the highest (A, receives funding).
    assert weights == {"B": Decimal(1), "A": Decimal(-1)}


def test_carry_too_few_scored_takes_no_book() -> None:
    funding = _seq({"A": ["0.001"], "B": ["-0.001"]})  # 2 names, top_k=2 needs 4
    assert FundingCarry(CrossSectionalConfig(lookback=1, top_k=2)).target_weights(funding) == {}


def test_carry_excludes_name_with_too_short_funding_history() -> None:
    # C has fewer than `lookback` funding points -> no trailing mean -> it cannot be selected.
    funding = _seq({"A": ["0.001"] * 3, "B": ["-0.001"] * 3, "C": ["0.0"], "D": ["0.0005"] * 3})
    weights = FundingCarry(CrossSectionalConfig(lookback=3, top_k=1)).target_weights(funding)
    assert "C" not in weights


# --- the carry backtester: pure-carry P&L, costs, look-ahead -------------------------------------


def test_return_is_pure_carry_when_prices_are_flat() -> None:
    n = 6
    prices = {s: _flat_bars(s, n) for s in ("A", "B", "C", "D")}
    funding = {
        "A": _funding("A", ["0.001"] * n),  # highest -> shorted -> receives +0.001
        "B": _funding("B", ["-0.001"] * n),  # lowest -> longed -> paid +0.001
        "C": _funding("C", ["0"] * n),
        "D": _funding("D", ["0.0005"] * n),
    }
    returns = list(_bt(prices, funding).run(_proposal(_PARAMS)))
    # flat prices -> 0 price P&L; carry = -w_B*f_B - w_A*f_A = +0.001 (long B) + 0.001 (short A).
    assert returns and all(r == pytest.approx(0.002) for r in returns)


def test_carry_uses_next_day_funding_not_the_current_day() -> None:
    # pins the i+1 carry timing: A's funding rises day by day while it stays the highest (shorted),
    # so each bar's carry must use the NEXT day's funding (a regression to funding[i] would shift).
    n = 6
    prices = {s: _flat_bars(s, n) for s in ("A", "B", "C", "D")}
    funding = {
        "A": _funding(
            "A", ["0.010", "0.011", "0.012", "0.013", "0.014", "0.015"]
        ),  # highest -> short
        "B": _funding("B", ["-0.010"] * n),  # lowest -> long
        "C": _funding("C", ["0.001"] * n),
        "D": _funding("D", ["0.002"] * n),
    }
    # held = {B:+1, A:-1}; carry[i] = -f_B[i+1] + f_A[i+1] = 0.010 + A[i+1]; i in {1,2,3,4}.
    returns = list(_bt(prices, funding).run(_proposal(_PARAMS)))
    assert returns == pytest.approx([0.022, 0.023, 0.024, 0.025])


def test_return_composes_price_move_and_carry() -> None:
    # pins price + carry together: long B (price x0.9 -> -0.1), short A (price x1.1 -> +0.1) -> the
    # price book is -0.2/bar; carry +0.02/bar; return -0.18/bar.
    prices = {
        "A": _price_bars("A", ["100", "110", "121", "133.1", "146.41", "161.051"]),  # x1.1, shorted
        "B": _price_bars("B", ["100", "90", "81", "72.9", "65.61", "59.049"]),  # x0.9, longed
        "C": _flat_bars("C", 6),
        "D": _flat_bars("D", 6),
    }
    funding = {
        "A": _funding("A", ["0.01"] * 6),  # highest -> short
        "B": _funding("B", ["-0.01"] * 6),  # lowest -> long
        "C": _funding("C", ["0.001"] * 6),
        "D": _funding("D", ["0.002"] * 6),
    }
    returns = list(_bt(prices, funding).run(_proposal(_PARAMS)))
    assert returns == pytest.approx([-0.18] * 4)  # price (-0.2) + carry (+0.02)


def test_carry_requires_a_daily_panel() -> None:
    # daily_funding buckets to UTC days -> an intraday panel must fail loud, not undercount.
    hourly = {s: _flat_bars(s, 30, interval=timedelta(hours=1)) for s in ("A", "B", "C", "D")}
    funding = {s: _funding(s, ["0.0001"] * 30) for s in ("A", "B", "C", "D")}
    with pytest.raises(NotImplementedError, match="daily panel"):
        _bt(hourly, funding).run(_proposal(_PARAMS))


def test_carry_holding_period_holds_book_between_rebalances() -> None:
    n = 8
    prices = {s: _flat_bars(s, n) for s in ("A", "B", "C", "D")}
    funding = {
        "A": _funding("A", ["0.001"] * n),
        "B": _funding("B", ["-0.001"] * n),
        "C": _funding("C", ["0"] * n),
        "D": _funding("D", ["0.0005"] * n),
    }
    # holding_period=2 exercises the hold-between-rebalances branch; constant funding -> the same
    # book each re-rank, so every bar still earns the pure carry (+0.002).
    returns = list(
        _bt(prices, funding).run(_proposal({"lookback": 1, "top_k": 1, "holding_period": 2}))
    )
    assert returns and all(r == pytest.approx(0.002) for r in returns)


def test_turnover_cost_reduces_the_first_rebalance() -> None:
    n = 6
    prices = {s: _flat_bars(s, n) for s in ("A", "B", "C", "D")}
    funding = {
        "A": _funding("A", ["0.001"] * n),
        "B": _funding("B", ["-0.001"] * n),
        "C": _funding("C", ["0"] * n),
        "D": _funding("D", ["0.0005"] * n),
    }
    gross = list(_bt(prices, funding, cost=Decimal(0)).run(_proposal(_PARAMS)))
    net = list(_bt(prices, funding, cost=Decimal("0.01")).run(_proposal(_PARAMS)))
    assert net[0] < gross[0]  # building the book from flat -> turnover -> cost
    assert all(x <= y for x, y in zip(net, gross, strict=True))


def test_carry_no_lookahead_future_bar_leaves_earlier_returns_unchanged() -> None:
    n = 6
    prices = {s: _flat_bars(s, n) for s in ("A", "B", "C", "D")}
    base_f = {
        "A": ["0.003"] * n,
        "B": ["-0.003"] * n,
        "C": ["0.001"] * n,
        "D": ["-0.001"] * n,
    }
    base = list(_bt(prices, {s: _funding(s, r) for s, r in base_f.items()}).run(_proposal(_PARAMS)))
    bumped_f = {**base_f, "A": [*base_f["A"][:-1], "9"]}  # change only A's final-day funding
    bumped = list(
        _bt(prices, {s: _funding(s, r) for s, r in bumped_f.items()}).run(_proposal(_PARAMS))
    )
    assert base[:-1] == bumped[:-1]  # only the last carry (which uses the last day) may move


def test_too_few_aligned_bars_raises() -> None:
    prices = {s: _flat_bars(s, 3) for s in ("A", "B", "C", "D")}
    funding = {s: _funding(s, ["0.0001"] * 3) for s in ("A", "B", "C", "D")}
    bt = FundingPanelBacktester(
        panel_bars_for=lambda _m, _w: prices,
        panel_funding_for=lambda _m, _w: funding,
        cost_fraction=Decimal(0),  # default min_bars (the rigor floor)
    )
    with pytest.raises(ValueError, match="too few aligned"):
        bt.run(_proposal(_PARAMS))


def test_unknown_carry_template_raises() -> None:
    bt = _bt({}, {})
    with pytest.raises(ValueError, match="unknown carry template"):
        bt.run(StrategyProposal("cross_sectional_momentum", _PARAMS, _CRYPTO, "p", 1, "fp"))


def test_carry_template_registered() -> None:
    assert "cross_sectional_carry" in FUNDING_TEMPLATES


# --- the funding panel source --------------------------------------------------------------------


def test_cold_store_funding_reads_only_ingested_members(tmp_path: object) -> None:
    store = FundingStore(tmp_path)  # type: ignore[arg-type]
    store.write(_funding("BTCUSDT", ["0.001", "0.001"]) + _funding("ETHUSDT", ["-0.001", "-0.001"]))
    members = ColdStoreFundingFor.from_config(store)(_CRYPTO, "crypto-perps-1d")
    assert set(members) == {"BTCUSDT", "ETHUSDT"}  # the other 40 read empty -> dropped


def test_cold_store_funding_unmapped_raises(tmp_path: object) -> None:
    with pytest.raises(ValueError, match="no discovery panel mapped"):
        ColdStoreFundingFor(FundingStore(tmp_path), {})(_CRYPTO, "not-a-panel")  # type: ignore[arg-type]
