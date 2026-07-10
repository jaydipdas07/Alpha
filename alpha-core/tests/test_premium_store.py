"""PremiumStore + premiumIndexKlines CSV parse tests — round-trip, last-wins,
header/no-header eras, ms/µs stamps, bar-END keying."""

from __future__ import annotations

import io
import math
from pathlib import Path

import pytest

from alpha_core.core.enums import Venue
from alpha_core.data.ingest.binance_premium import parse_premium_kline_csv
from alpha_core.data.premium_store import PremiumRow, PremiumStore


def _row(epoch: int, close: float) -> PremiumRow:
    return (epoch, close, close, close, close)


class TestPremiumStore:
    def test_round_trip(self, tmp_path: Path) -> None:
        store = PremiumStore(tmp_path / "prem")
        n = store.write_month(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            month="2026-01",
            rows=[_row(1_767_225_660, 0.0001), _row(1_767_225_720, -0.0002)],
        )
        assert n == 2
        span = store.read_span(venue=Venue.BINANCE, symbol="BTCUSDT")
        assert list(span.epoch_s) == [1_767_225_660, 1_767_225_720]
        assert span.premium_close[0] == 0.0001
        assert span.premium_close[1] == -0.0002  # negative premium is legitimate

    def test_merge_is_idempotent_last_wins(self, tmp_path: Path) -> None:
        store = PremiumStore(tmp_path / "prem")
        store.write_month(
            venue=Venue.BINANCE, symbol="BTCUSDT", month="2026-01", rows=[_row(1_767_225_660, 1.0)]
        )
        store.write_month(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            month="2026-01",
            rows=[_row(1_767_225_660, 2.0), _row(1_767_225_720, 3.0)],
        )
        span = store.read_span(venue=Venue.BINANCE, symbol="BTCUSDT")
        assert len(span.epoch_s) == 2
        assert span.premium_close[0] == 2.0  # re-ingest wins

    def test_ascending_assert_across_partitions(self, tmp_path: Path) -> None:
        store = PremiumStore(tmp_path / "prem")
        store.write_month(
            venue=Venue.BINANCE, symbol="BTCUSDT", month="2026-02", rows=[_row(1_767_225_660, 1.0)]
        )
        store.write_month(
            venue=Venue.BINANCE, symbol="BTCUSDT", month="2026-01", rows=[_row(1_767_225_720, 1.0)]
        )
        with pytest.raises(ValueError, match="ascending"):
            store.read_span(venue=Venue.BINANCE, symbol="BTCUSDT")


class TestParsePremiumKlineCsv:
    ROW_2026 = (
        "1780272000000,-0.00023142,-0.00023142,-0.00058864,-0.00035791,0,1780272059999,0,12,0,0,0\n"
    )

    def test_headerless_era_parses(self) -> None:
        rows, stats = parse_premium_kline_csv(io.StringIO(self.ROW_2026))
        assert stats.csv_rows == 1
        assert stats.bad_rows == 0
        (row,) = rows
        # bar-END keying: open 1780272000 s + 60
        assert row[0] == 1_780_272_060
        assert row[1] == -0.00023142  # open
        assert row[4] == -0.00035791  # close

    def test_header_era_skips_header(self) -> None:
        csv_text = (
            "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
            "taker_buy_volume,taker_buy_quote_volume,ignore\n" + self.ROW_2026
        )
        rows, stats = parse_premium_kline_csv(io.StringIO(csv_text))
        assert stats.csv_rows == 1  # the header is not a row
        assert len(rows) == 1

    def test_microsecond_stamps_accepted(self) -> None:
        row_us = "1780272000000000,0.0001,0.0002,0.0000,0.0001,0,1780272059999999,0,12,0,0,0\n"
        rows, _ = parse_premium_kline_csv(io.StringIO(row_us))
        assert rows[0][0] == 1_780_272_060  # same bar END as the ms stamp

    def test_bad_and_nonfinite_rows_counted(self) -> None:
        csv_text = (
            "not-a-stamp,0.1,0.1,0.1,0.1,0,0,0,0,0,0,0\n"
            "1780272000000,0.1,inf,0.1,0.1,0,0,0,0,0,0,0\n"
            "1780272000000,0.1\n" + self.ROW_2026
        )
        rows, stats = parse_premium_kline_csv(io.StringIO(csv_text))
        assert stats.bad_rows == 3
        assert len(rows) == 1

    def test_duplicate_open_time_last_wins(self) -> None:
        dup = "1780272000000,0.9,0.9,0.9,0.9,0,1780272059999,0,12,0,0,0\n"
        rows, _ = parse_premium_kline_csv(io.StringIO(self.ROW_2026 + dup))
        (row,) = rows
        assert row[4] == 0.9

    def test_nan_premium_is_bad_row(self) -> None:
        row = "1780272000000,nan,0.1,0.1,0.1,0,0,0,0,0,0,0\n"
        rows, stats = parse_premium_kline_csv(io.StringIO(row))
        assert stats.bad_rows == 1
        assert not rows
        assert math.isnan(float("nan"))  # sanity: float('nan') parses, isfinite rejects
