"""Ingest transform tests (B1a.1b) — pure row/JSON -> Bar, no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.data.ingest.binance import aggtrades_to_bars, kline_to_bar, klines_to_bars
from alpha_core.data.ingest.kite import access_token_is_stale, candles_to_bars
from alpha_core.data.ingest.yahoo import chart_to_bars

_IST = timezone(timedelta(hours=5, minutes=30))  # Kite returns IST timestamps

# A real-shaped Binance fapi 5m kline (openTime ms, then string OHLCV, closeTime, ...).
_KLINE = [1719360000000, "60000.00", "60100.50", "59900.10", "60050.25", "123.456", 1719360299999]


def test_binance_kline_to_bar_is_decimal_and_utc() -> None:
    bar = kline_to_bar(_KLINE, symbol="BTCUSDT", interval_seconds=300)
    assert bar.symbol == "BTCUSDT"
    assert bar.venue is Venue.BINANCE and bar.asset_class is AssetClass.CRYPTO
    assert bar.start == datetime(2024, 6, 26, 0, 0, tzinfo=UTC)
    assert bar.interval == timedelta(minutes=5)
    assert (bar.open, bar.high, bar.low, bar.close) == (
        Decimal("60000.00"),
        Decimal("60100.50"),
        Decimal("59900.10"),
        Decimal("60050.25"),
    )
    assert bar.volume == Decimal("123.456")
    assert isinstance(bar.close, Decimal)  # money is never a float


def test_binance_klines_to_bars_pages() -> None:
    page = [_KLINE, [1719360300000, "60050.25", "60080", "60010", "60070", "10.5", 1719360599999]]
    bars = klines_to_bars(page, symbol="BTCUSDT", interval_seconds=300)
    assert [b.start for b in bars] == [
        datetime(2024, 6, 26, 0, 0, tzinfo=UTC),
        datetime(2024, 6, 26, 0, 5, tzinfo=UTC),
    ]


# A real-shaped archive kline CSV (string fields) with a header row to skip.
_ARCHIVE_KLINE_CSV = [
    ["open_time", "open", "high", "low", "close", "volume", "close_time"],  # header
    ["1719360000000", "60000.00", "60100.50", "59900.10", "60050.25", "123.456", "1719360000999"],
]

# A real-shaped archive aggTrade block: [aggId, price, qty, firstId, lastId, ts_ms, isBuyerMaker].
_AGGTRADES = [
    [
        "agg_trade_id",
        "price",
        "quantity",
        "first",
        "last",
        "transact_time",
        "is_buyer_maker",
    ],  # header
    ["1", "100.0", "1.0", "10", "10", "1719360000100", "true"],  # bucket A (00:00:00)
    ["2", "101.0", "2.0", "11", "11", "1719360000500", "false"],
    ["3", "99.5", "0.5", "12", "12", "1719360000900", "true"],
    ["4", "99.5", "1.0", "13", "13", "1719360001000", "false"],  # bucket B (00:00:01)
    ["5", "100.5", "1.5", "14", "14", "1719360001800", "true"],
]


def test_klines_to_bars_skips_archive_csv_header() -> None:
    bars = klines_to_bars(_ARCHIVE_KLINE_CSV, symbol="BTCUSDT", interval_seconds=1)
    assert len(bars) == 1  # the header row is skipped, the data row parsed
    assert bars[0].close == Decimal("60050.25") and isinstance(bars[0].close, Decimal)


def test_aggtrades_to_bars_folds_ohlcv_per_bucket() -> None:
    bars = aggtrades_to_bars(_AGGTRADES, symbol="BTCUSDT", interval_seconds=1)
    assert len(bars) == 2
    a, b = bars
    assert a.start == datetime(2024, 6, 26, 0, 0, 0, tzinfo=UTC)
    assert (a.open, a.high, a.low, a.close, a.volume) == (
        Decimal("100.0"),
        Decimal("101.0"),
        Decimal("99.5"),
        Decimal("99.5"),
        Decimal("3.5"),
    )
    assert b.start == datetime(2024, 6, 26, 0, 0, 1, tzinfo=UTC)
    assert (b.open, b.high, b.low, b.close, b.volume) == (
        Decimal("99.5"),
        Decimal("100.5"),
        Decimal("99.5"),
        Decimal("100.5"),
        Decimal("2.5"),
    )
    assert a.interval == timedelta(seconds=1)
    assert all(isinstance(x, Decimal) for x in (a.open, a.volume))  # money never float


def test_aggtrades_to_bars_empty_is_empty() -> None:
    assert aggtrades_to_bars([], symbol="BTCUSDT", interval_seconds=1) == []
    # a header-only stream yields no bars either
    assert aggtrades_to_bars([_AGGTRADES[0]], symbol="BTCUSDT", interval_seconds=1) == []


def test_aggtrades_to_bars_gap_makes_no_phantom_bar() -> None:
    # Trades only at second 0 and second 5 -> exactly 2 bars, no empty buckets in between.
    rows = [
        ["1", "100", "1", "0", "0", "1719360000000", "true"],  # 00:00:00
        ["2", "105", "2", "0", "0", "1719360005000", "true"],  # 00:00:05 (4s gap)
    ]
    bars = aggtrades_to_bars(rows, symbol="BTCUSDT", interval_seconds=1)
    assert [b.start.second for b in bars] == [0, 5]  # no phantom 1..4
    assert bars[0].volume == Decimal("1") and bars[1].open == Decimal("105")  # single-trade bucket


def test_aggtrades_to_bars_rejects_out_of_order() -> None:
    rows = [
        ["1", "100", "1", "0", "0", "1719360005000", "true"],  # 00:00:05
        ["2", "101", "1", "0", "0", "1719360000000", "true"],  # 00:00:00 — goes backwards
    ]
    with pytest.raises(ValueError, match="time-ordered"):
        aggtrades_to_bars(rows, symbol="BTCUSDT", interval_seconds=1)


def test_yahoo_chart_to_bars_skips_null_days() -> None:
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1719360000, 1719446400, 1719532800],
                    "indicators": {
                        "quote": [
                            {
                                "open": [2900.0, None, 2925.5],  # middle day = holiday (null)
                                "high": [2950.0, None, 2960.0],
                                "low": [2890.0, None, 2910.0],
                                "close": [2940.0, None, 2955.0],
                                "volume": [1234567, None, 2345678],
                            }
                        ]
                    },
                }
            ]
        }
    }
    bars = chart_to_bars(payload, symbol="NSE:RELIANCE")
    assert len(bars) == 2  # the null day is dropped
    assert bars[0].venue is Venue.NSE and bars[0].asset_class is AssetClass.EQUITY
    assert bars[0].interval == timedelta(days=1)
    assert (bars[0].open, bars[0].close, bars[0].volume) == (
        Decimal("2900.0"),
        Decimal("2940.0"),
        Decimal("1234567"),
    )
    assert bars[1].close == Decimal("2955.0")


def _kite_candle(minute: int, **ov: object) -> dict[str, object]:
    row: dict[str, object] = {
        "date": datetime(2024, 6, 26, 9, minute, tzinfo=_IST),
        "open": 2900.0,
        "high": 2950.0,
        "low": 2890.0,
        "close": 2940.0,
        "volume": 12345,
    }
    row.update(ov)
    return row


def test_kite_candles_to_bars_ist_to_utc_and_decimal() -> None:
    bars = candles_to_bars(
        [_kite_candle(15), _kite_candle(16, close=2935.5, volume=6789)],
        symbol="NSE:RELIANCE",
        interval_seconds=60,
    )
    assert len(bars) == 2
    assert bars[0].venue is Venue.NSE and bars[0].asset_class is AssetClass.EQUITY
    assert bars[0].start == datetime(2024, 6, 26, 3, 45, tzinfo=UTC)  # 09:15 IST -> 03:45 UTC
    assert bars[1].start == datetime(2024, 6, 26, 3, 46, tzinfo=UTC)
    assert bars[0].interval == timedelta(minutes=1)
    assert (bars[0].open, bars[0].close, bars[0].volume) == (
        Decimal("2900.0"),
        Decimal("2940.0"),
        Decimal("12345"),
    )
    assert isinstance(bars[0].close, Decimal)  # money is never a float
    assert bars[1].close == Decimal("2935.5")


def test_kite_candles_skips_incomplete_rows() -> None:
    bars = candles_to_bars(
        [_kite_candle(15), _kite_candle(16, open=None)],  # second row missing OHLCV
        symbol="NSE:RELIANCE",
        interval_seconds=60,
    )
    assert len(bars) == 1


def test_kite_candles_rejects_naive_date() -> None:
    naive = {
        "date": datetime(2024, 6, 26, 9, 15),
        "open": 1.0,
        "high": 1.0,
        "low": 1.0,
        "close": 1.0,
        "volume": 1,
    }
    with pytest.raises(ValueError, match="tz-aware"):
        candles_to_bars([naive], symbol="NSE:RELIANCE", interval_seconds=60)


def test_kite_candles_empty_is_empty() -> None:
    assert candles_to_bars([], symbol="NSE:RELIANCE", interval_seconds=60) == []


# --- Kite daily-token staleness (the 06:00-IST rollover predicate) ---------------------------


def test_kite_token_fresh_same_day_after_expiry() -> None:
    # Minted at 09:00 IST; checked the same afternoon -> no 06:00 boundary crossed -> fresh.
    token_at = datetime(2026, 6, 29, 9, 0, tzinfo=_IST)
    now = datetime(2026, 6, 29, 14, 0, tzinfo=_IST)
    assert access_token_is_stale(token_at, now=now) is False


def test_kite_token_stale_after_6am_rollover() -> None:
    # Minted yesterday morning; checked past today's 06:00 IST -> the boundary was crossed -> stale.
    token_at = datetime(2026, 6, 28, 9, 0, tzinfo=_IST)
    now = datetime(2026, 6, 29, 7, 0, tzinfo=_IST)
    assert access_token_is_stale(token_at, now=now) is True


def test_kite_token_fresh_before_todays_expiry() -> None:
    # Minted yesterday morning, checked at 05:00 IST today (before 06:00) -> the live boundary is
    # *yesterday's* 06:00, which the token post-dates -> still fresh.
    token_at = datetime(2026, 6, 28, 9, 0, tzinfo=_IST)
    now = datetime(2026, 6, 29, 5, 0, tzinfo=_IST)
    assert access_token_is_stale(token_at, now=now) is False


def test_kite_token_minted_at_boundary_is_fresh() -> None:
    # A token minted exactly at the 06:00 IST boundary is valid for that day (strict <, not <=).
    token_at = datetime(2026, 6, 29, 6, 0, tzinfo=_IST)
    now = datetime(2026, 6, 29, 10, 0, tzinfo=_IST)
    assert access_token_is_stale(token_at, now=now) is False


def test_kite_token_staleness_handles_utc_inputs() -> None:
    # The .env stores UTC; the predicate converts to IST. 2026-06-28 04:00Z = 09:30 IST (28th),
    # 2026-06-29 02:00Z = 07:30 IST (29th) -> a 06:00-IST rollover (the 29th) sits between -> stale.
    token_at = datetime(2026, 6, 28, 4, 0, tzinfo=UTC)
    now = datetime(2026, 6, 29, 2, 0, tzinfo=UTC)
    assert access_token_is_stale(token_at, now=now) is True


def test_kite_token_staleness_rejects_naive() -> None:
    aware = datetime(2026, 6, 29, 10, 0, tzinfo=UTC)
    naive = datetime(2026, 6, 29, 10, 0)  # intentionally naive, to assert the tz-aware guard
    with pytest.raises(ValueError, match="tz-aware"):
        access_token_is_stale(naive, now=aware)
    with pytest.raises(ValueError, match="tz-aware"):
        access_token_is_stale(aware, now=naive)
