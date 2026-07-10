"""DepthStore + bookDepth CSV parse tests — round-trip, last-wins, honest-NaN, bands."""

from __future__ import annotations

import io
import math
from pathlib import Path

import numpy as np
import pytest

from alpha_core.core.enums import Venue
from alpha_core.data.depth_store import BANDS, DepthRow, DepthStore
from alpha_core.data.ingest.binance_depth import parse_book_depth_csv

_N = len(BANDS)


def _row(epoch: int, bid02: float, ask02: float) -> DepthRow:
    vals = [math.nan] * (2 * _N)
    vals[0] = bid02
    vals[_N] = ask02
    return (epoch, *vals)  # type: ignore[return-value]


class TestDepthStore:
    def test_round_trip_and_shape(self, tmp_path: Path) -> None:
        store = DepthStore(tmp_path / "depth")
        n = store.write_month(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            month="2026-01",
            rows=[_row(1_767_225_600, 800.0, 200.0), _row(1_767_225_630, 700.0, 300.0)],
        )
        assert n == 2
        span = store.read_span(venue=Venue.BINANCE, symbol="BTCUSDT")
        assert span.bid.shape == (2, _N)
        assert span.ask.shape == (2, _N)
        assert list(span.epoch_s) == [1_767_225_600, 1_767_225_630]
        assert span.bid[0, 0] == 800.0
        assert span.ask[1, 0] == 300.0
        # honest-NaN: unpublished bands stay NaN, never zero
        assert np.isnan(span.bid[0, 1:]).all()
        assert np.isnan(span.ask[0, 1:]).all()

    def test_merge_is_idempotent_last_wins(self, tmp_path: Path) -> None:
        store = DepthStore(tmp_path / "depth")
        store.write_month(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            month="2026-01",
            rows=[_row(1_767_225_600, 800.0, 200.0)],
        )
        store.write_month(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            month="2026-01",
            rows=[_row(1_767_225_600, 750.0, 250.0), _row(1_767_225_630, 1.0, 1.0)],
        )
        span = store.read_span(venue=Venue.BINANCE, symbol="BTCUSDT")
        assert len(span.epoch_s) == 2
        assert span.bid[0, 0] == 750.0  # re-ingest wins
        assert span.ask[0, 0] == 250.0

    def test_ascending_assert_across_partitions(self, tmp_path: Path) -> None:
        store = DepthStore(tmp_path / "depth")
        store.write_month(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            month="2026-02",
            rows=[_row(1_767_225_600, 1.0, 1.0)],  # a January epoch misfiled into February
        )
        store.write_month(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            month="2026-01",
            rows=[_row(1_767_225_630, 1.0, 1.0)],
        )
        with pytest.raises(ValueError, match="ascending"):
            store.read_span(venue=Venue.BINANCE, symbol="BTCUSDT")

    def test_empty_series_reads_empty(self, tmp_path: Path) -> None:
        store = DepthStore(tmp_path / "depth")
        span = store.read_span(venue=Venue.BINANCE, symbol="BTCUSDT")
        assert len(span.epoch_s) == 0
        assert span.bid.shape == (0, _N)


class TestParseBookDepthCsv:
    HEADER = "timestamp,percentage,depth,notional\n"

    def test_pivots_one_snapshot(self) -> None:
        csv_text = self.HEADER + "".join(
            f"2026-07-01 00:00:04,{pct},1.0,{notional}\n"
            for pct, notional in [
                ("-0.20", "100.5"),
                ("-1.00", "200.5"),
                ("0.20", "50.25"),
                ("5.00", "999.0"),
            ]
        )
        rows, stats = parse_book_depth_csv(io.StringIO(csv_text))
        assert stats.csv_rows == 4
        assert stats.snapshots == 1
        assert not stats.unknown_bands
        (row,) = rows
        epoch, vals = row[0], row[1:]
        assert epoch == 1_782_864_004  # 2026-07-01T00:00:04Z
        assert vals[0] == 100.5  # bid 0.2
        assert vals[1] == 200.5  # bid 1.0
        assert vals[_N] == 50.25  # ask 0.2
        assert vals[2 * _N - 1] == 999.0  # ask 5.0
        assert math.isnan(vals[2])  # unpublished band: honest NaN

    def test_last_wins_and_multiple_snapshots(self) -> None:
        csv_text = self.HEADER + (
            "2026-07-01 00:00:04,-0.20,1.0,100.0\n"
            "2026-07-01 00:00:04,-0.20,1.0,111.0\n"  # duplicate (ts, band): last wins
            "2026-07-01 00:00:34,-0.20,1.0,222.0\n"
        )
        rows, stats = parse_book_depth_csv(io.StringIO(csv_text))
        assert stats.snapshots == 2
        assert rows[0][1] == 111.0
        assert rows[1][1] == 222.0
        assert rows[0][0] < rows[1][0]  # sorted output

    def test_unknown_band_reported_not_fatal(self) -> None:
        csv_text = self.HEADER + (
            "2026-07-01 00:00:04,-10.00,1.0,100.0\n2026-07-01 00:00:04,-0.20,1.0,50.0\n"
        )
        rows, stats = parse_book_depth_csv(io.StringIO(csv_text))
        assert stats.unknown_bands == {10.0}
        assert len(rows) == 1
        assert rows[0][1] == 50.0

    def test_bad_rows_counted(self) -> None:
        csv_text = self.HEADER + (
            "not-a-date,-0.20,1.0,100.0\n"
            "2026-07-01 00:00:04,-0.20,1.0,not-a-number\n"
            "2026-07-01 00:00:04,-0.20,1.0,inf\n"
            "2026-07-01 00:00:04,-1.00,1.0,-5.0\n"
            "2026-07-01 00:00:04,0.20,1.0,42.0\n"
        )
        rows, stats = parse_book_depth_csv(io.StringIO(csv_text))
        assert stats.bad_rows == 4  # bad date, bad number, non-finite, negative notional
        assert len(rows) == 1
        assert rows[0][1 + _N] == 42.0
