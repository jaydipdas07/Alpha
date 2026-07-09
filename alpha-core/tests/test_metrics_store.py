"""MetricsStore tests — NaN-preserving round-trip, last-wins merge, span ordering."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from alpha_core.core.enums import Venue
from alpha_core.data.metrics_store import MetricsRow, MetricsStore


def _row(epoch: int, oi: float, *, ratio: float = 1.25) -> MetricsRow:
    return (epoch, oi, oi * 50_000.0, ratio, ratio, ratio, ratio)


def test_round_trip_preserves_nan_ratios(tmp_path: Path) -> None:
    """The 2022 archive era publishes OI only — NaN ratios must survive the store."""
    store = MetricsStore(tmp_path)
    rows = [
        _row(1_700_000_000, 1000.0),
        (1_700_000_300, 1001.0, 5.0e7, math.nan, math.nan, math.nan, math.nan),
    ]
    assert store.write_month(venue=Venue.BINANCE, symbol="BTCUSDT", month="2023-11", rows=rows) == 2
    span = store.read_span(venue=Venue.BINANCE, symbol="BTCUSDT")
    assert span.epoch_s.tolist() == [1_700_000_000, 1_700_000_300]
    assert span.open_interest.tolist() == [1000.0, 1001.0]
    assert span.top_ls_accounts[0] == 1.25
    assert math.isnan(span.top_ls_accounts[1])
    assert math.isnan(span.taker_buy_sell_ratio[1])


def test_write_month_merges_last_wins(tmp_path: Path) -> None:
    store = MetricsStore(tmp_path)
    store.write_month(
        venue=Venue.BINANCE,
        symbol="BTCUSDT",
        month="2023-11",
        rows=[_row(1_700_000_000, 1000.0), _row(1_700_000_300, 1001.0)],
    )
    # re-ingest overwrites the shared epoch and appends a new one
    n = store.write_month(
        venue=Venue.BINANCE,
        symbol="BTCUSDT",
        month="2023-11",
        rows=[_row(1_700_000_300, 2002.0), _row(1_700_000_600, 1002.0)],
    )
    assert n == 3
    span = store.read_span(venue=Venue.BINANCE, symbol="BTCUSDT")
    assert span.epoch_s.tolist() == [1_700_000_000, 1_700_000_300, 1_700_000_600]
    assert span.open_interest.tolist() == [1000.0, 2002.0, 1002.0]
    assert store.months(venue=Venue.BINANCE, symbol="BTCUSDT") == ["2023-11"]


def test_read_span_rejects_out_of_order_partitions(tmp_path: Path) -> None:
    """A row filed under the wrong month must fail the strictly-ascending assert, not
    silently feed a shuffled tape to the folds."""
    store = MetricsStore(tmp_path)
    store.write_month(
        venue=Venue.BINANCE, symbol="BTCUSDT", month="2023-11", rows=[_row(1_700_000_600, 1.0)]
    )
    store.write_month(
        venue=Venue.BINANCE, symbol="BTCUSDT", month="2023-12", rows=[_row(1_700_000_300, 2.0)]
    )
    with pytest.raises(ValueError, match="strictly ascending"):
        store.read_span(venue=Venue.BINANCE, symbol="BTCUSDT")


def test_empty_series_reads_empty(tmp_path: Path) -> None:
    span = MetricsStore(tmp_path).read_span(venue=Venue.BINANCE, symbol="NOPE")
    assert len(span.epoch_s) == 0
    assert span.epoch_s.dtype == np.int64
