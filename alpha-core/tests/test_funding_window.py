"""Funding-window fold (FW family) — look-ahead discipline, window timing, costs, gap
safety, and the pre-registered spaces."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.funding import FundingRate, FundingStore
from alpha_core.helpers.config import DiscoveryCellConfig
from alpha_core.research.funding_window_backtester import (
    FUNDING_WINDOW_CELLS,
    FUNDING_WINDOW_TEMPLATES,
    FundingWindowBacktester,
    taker_cost_per_side,
)
from alpha_core.research.promote import make_proposal
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.strategist import (
    CellSaturated,
    RandomProposer,
    Strategist,
    StrategyProposal,
)

_T0 = datetime(2025, 3, 1, 0, 0, tzinfo=UTC)  # a funding boundary (00:00 UTC)
_SYMBOL = "LINKUSDT"
_WINDOW = "linkusdt-1m-fw"


def _bar(i: int, close: float) -> Bar:
    c = Decimal(str(close))
    return Bar(
        symbol=_SYMBOL,
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        start=_T0 + timedelta(minutes=i),
        interval=timedelta(minutes=1),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=Decimal("10"),
    )


def _funding(tmp_path: Path, rates: list[tuple[int, str]]) -> FundingStore:
    """A FundingStore with events at ``_T0 + minutes`` holding the given rates."""
    store = FundingStore(tmp_path / "funding")
    store.write(
        [
            FundingRate(
                symbol=_SYMBOL,
                venue=Venue.BINANCE,
                funding_time=_T0 + timedelta(minutes=m),
                rate=Decimal(r),
            )
            for m, r in rates
        ]
    )
    return store


def _proposal(template: str, params: dict[str, object]) -> StrategyProposal:
    cell = DiscoveryCellConfig(
        market=AssetClass.CRYPTO,
        window=_WINDOW,
        symbol=_SYMBOL,
        venue=Venue.BINANCE,
        interval_seconds=60,
        starting_cash=Decimal("1000000"),
    )
    return make_proposal(template, params, cell, trial_index=1)  # type: ignore[arg-type]


def _dip_and_rebound_bars() -> list[Bar]:
    """Flat 100 -> linear dip into the funding event at minute 960 -> linear recovery after.

    Events at minute 480 (prev, sets the conditioning rate) and 960 (the traded window).
    """
    closes = []
    for i in range(1500):
        if 900 <= i < 960:  # 60m pre-window drift down into the event
            closes.append(100.0 - (i - 900) * 0.05)  # -3% into the settlement
        elif 960 <= i < 1020:  # 60m rebound after
            closes.append(97.0 + (i - 960) * 0.05)
        elif i >= 1020:
            closes.append(100.0)
        else:
            closes.append(100.0)
    return [_bar(i, c) for i, c in enumerate(closes)]


def test_pre_drift_shorts_into_positive_funding_and_profits(tmp_path: Path) -> None:
    funding = _funding(tmp_path, [(480, "0.0005"), (960, "0.0005")])
    bt = FundingWindowBacktester(lambda s: _dip_and_rebound_bars(), funding)
    ret = np.asarray(
        bt.run(_proposal("funding_pre_drift", {"pre_minutes": 60, "min_funding_bps": Decimal("1")}))
    )
    # the short over the -3% dip earns ~+3% minus two taker sides
    window = ret[900:960]
    assert window.sum() > 0.02
    assert ret[:895].sum() == 0.0 and ret[1025:].sum() == 0.0  # nothing outside windows


def test_pre_drift_conditions_on_previous_rate_only(tmp_path: Path) -> None:
    # prev rate ~0 (below the 1bp bar), the rate AT the event huge: trading the pre-window
    # off the event's own rate would be look-ahead — the fold must stay flat.
    funding = _funding(tmp_path, [(480, "0.000001"), (960, "0.01")])
    bt = FundingWindowBacktester(lambda s: _dip_and_rebound_bars(), funding)
    ret = np.asarray(
        bt.run(_proposal("funding_pre_drift", {"pre_minutes": 60, "min_funding_bps": Decimal("1")}))
    )
    assert ret[890:960].sum() == 0.0


def test_rebound_enters_one_bar_after_settlement_and_profits(tmp_path: Path) -> None:
    funding = _funding(tmp_path, [(480, "0.0005"), (960, "0.0005")])
    bt = FundingWindowBacktester(lambda s: _dip_and_rebound_bars(), funding)
    ret = np.asarray(
        bt.run(_proposal("funding_rebound", {"post_minutes": 60, "min_funding_bps": Decimal("1")}))
    )
    # the settlement-minute bar itself carries no position (entry deferred one full bar)
    assert ret[960] == 0.0
    assert ret[961:1022].sum() > 0.02  # long the recovery, net of costs
    # negative funding flips the side: the same recovery now LOSES
    funding_neg = _funding(tmp_path / "neg", [(480, "-0.0005"), (960, "-0.0005")])
    bt_neg = FundingWindowBacktester(lambda s: _dip_and_rebound_bars(), funding_neg)
    ret_neg = np.asarray(
        bt_neg.run(
            _proposal("funding_rebound", {"post_minutes": 60, "min_funding_bps": Decimal("1")})
        )
    )
    assert ret_neg[961:1022].sum() < 0.0


def test_costs_two_sides_per_traded_window(tmp_path: Path) -> None:
    # flat prices: the only nonzero P&L is the cost drag — exactly 2 sides per window
    flat = [_bar(i, 100.0) for i in range(1500)]
    funding = _funding(tmp_path, [(480, "0.0005"), (960, "0.0005")])
    bt = FundingWindowBacktester(lambda s: flat, funding)
    ret = np.asarray(
        bt.run(_proposal("funding_pre_drift", {"pre_minutes": 30, "min_funding_bps": Decimal("1")}))
    )
    # one traded window (the 960 event conditioned on the 480 rate)
    assert ret.sum() == pytest.approx(-2 * taker_cost_per_side())


def test_gap_at_entry_reference_skips_the_window(tmp_path: Path) -> None:
    bars = [b for b in _dip_and_rebound_bars() if not (880 <= (b.start - _T0).seconds // 60 < 902)]
    funding = _funding(tmp_path, [(480, "0.0005"), (960, "0.0005")])
    bt = FundingWindowBacktester(lambda s: bars, funding)
    ret = np.asarray(
        bt.run(_proposal("funding_pre_drift", {"pre_minutes": 60, "min_funding_bps": Decimal("1")}))
    )
    assert ret.sum() == 0.0  # stale entry reference across the gap -> no phantom trade


def test_unknown_cell_rejected(tmp_path: Path) -> None:
    funding = _funding(tmp_path, [(480, "0.0005")])
    bt = FundingWindowBacktester(lambda s: _dip_and_rebound_bars(), funding)
    cell = DiscoveryCellConfig(
        market=AssetClass.CRYPTO,
        window="nope-1m-fw",
        symbol="NOPE",
        venue=Venue.BINANCE,
        interval_seconds=60,
        starting_cash=Decimal("1000000"),
    )
    proposal = make_proposal(
        "funding_pre_drift",
        {"pre_minutes": 30, "min_funding_bps": Decimal("1")},
        cell,
        trial_index=1,
    )
    with pytest.raises(ValueError, match="unknown funding-window cell"):
        bt.run(proposal)


def test_registry_spaces_enumerate_four_each() -> None:
    assert set(FUNDING_WINDOW_CELLS) == {
        "linkusdt-1m-fw",
        "ltcusdt-1m-fw",
        "bchusdt-1m-fw",
        "etcusdt-1m-fw",
        "filusdt-1m-fw",
        "atomusdt-1m-fw",
    }
    with ProposalLedger() as ledger:
        strategist = Strategist(
            ledger,
            proposer=RandomProposer(seed=7),
            templates=FUNDING_WINDOW_TEMPLATES,
            max_attempts=500,
        )
        for name in FUNDING_WINDOW_TEMPLATES:
            count = 0
            while True:
                try:
                    strategist.propose(name, market=AssetClass.CRYPTO, window=_WINDOW)
                except CellSaturated:
                    break
                count += 1
            assert count == 4, f"{name}: pre-registered space changed size ({count} != 4)"


def test_invalid_configs_rejected() -> None:
    for name in FUNDING_WINDOW_TEMPLATES:
        template = FUNDING_WINDOW_TEMPLATES[name]
        with pytest.raises((ValueError, Exception)):
            template.build({next(iter(template.param_space)): Decimal("0")})
