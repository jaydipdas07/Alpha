"""Signed taker-flow fold + FlowStore tests (the G2 family's signal input)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from alpha_core.core.enums import Venue
from alpha_core.data.flow_store import FlowStore
from alpha_core.data.ingest.binance import aggtrades_to_flows

# Archive aggTrade row: [aggId, price, qty, firstId, lastId, transactTime_ms, isBuyerMaker]
_HEADER = ["agg_trade_id", "price", "quantity", "first", "last", "transact_time", "is_buyer_maker"]


def test_flow_fold_signs_by_aggressor_and_buckets_by_second() -> None:
    rows = [
        _HEADER,  # skipped
        ["1", "100", "2.0", "0", "0", "1719360000100", "false"],  # taker BUY  @ s0
        ["2", "100", "0.5", "0", "0", "1719360000900", "true"],  # taker SELL @ s0
        ["3", "101", "1.0", "0", "0", "1719360002000", "true"],  # taker SELL @ s2 (gap s1)
    ]
    flows = aggtrades_to_flows(rows, interval_seconds=1)
    assert flows == [
        (1719360000, 2.0, 0.5),
        (1719360002, 0.0, 1.0),  # one-sided bucket carries 0.0 on the other leg
    ]
    # no phantom bucket for the empty second s1
    assert len(flows) == 2


def test_flow_fold_rejects_out_of_order() -> None:
    rows = [
        ["1", "100", "1", "0", "0", "1719360002000", "true"],
        ["2", "100", "1", "0", "0", "1719360000000", "false"],
    ]
    try:
        aggtrades_to_flows(rows, interval_seconds=1)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "time-ordered" in str(exc)


def test_flow_store_round_trip_idempotent_and_sorted(tmp_path: Path) -> None:
    store = FlowStore(tmp_path)
    n = store.write_month(
        venue=Venue.BINANCE,
        symbol="BTCUSDT",
        interval_seconds=1,
        month="2024-07",
        rows=[(1719360002, 0.0, 1.0), (1719360000, 2.0, 0.5)],  # unsorted on purpose
    )
    assert n == 2
    # idempotent merge: overlapping second re-written (last wins) + one new second
    n2 = store.write_month(
        venue=Venue.BINANCE,
        symbol="BTCUSDT",
        interval_seconds=1,
        month="2024-07",
        rows=[(1719360002, 9.0, 9.0), (1719360005, 1.0, 0.0)],
    )
    assert n2 == 3
    m = store.read_month(venue=Venue.BINANCE, symbol="BTCUSDT", interval_seconds=1, month="2024-07")
    assert m.epoch_s.tolist() == [1719360000, 1719360002, 1719360005]  # sorted
    assert m.buy.tolist() == [2.0, 9.0, 1.0]  # last-wins on the overlap
    assert m.sell.tolist() == [0.5, 9.0, 0.0]
    assert m.epoch_s.dtype == np.int64 and m.buy.dtype == np.float64


def test_flow_store_span_concatenates_months(tmp_path: Path) -> None:
    store = FlowStore(tmp_path)
    store.write_month(
        venue=Venue.BINANCE,
        symbol="ETHUSDT",
        interval_seconds=1,
        month="2024-07",
        rows=[(1719360000, 1.0, 0.0)],
    )
    store.write_month(
        venue=Venue.BINANCE,
        symbol="ETHUSDT",
        interval_seconds=1,
        month="2024-08",
        rows=[(1722470400, 0.0, 2.0)],
    )
    span = store.read_span(venue=Venue.BINANCE, symbol="ETHUSDT", interval_seconds=1)
    assert span.epoch_s.tolist() == [1719360000, 1722470400]
    assert store.months(venue=Venue.BINANCE, symbol="ETHUSDT", interval_seconds=1) == [
        "2024-07",
        "2024-08",
    ]
    empty = store.read_span(venue=Venue.BINANCE, symbol="NOPE", interval_seconds=1)
    assert len(empty.epoch_s) == 0
