"""Ingest transform tests (B1a.1b) — pure row/JSON -> Bar, no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.data.ingest.binance import aggtrades_to_bars, kline_to_bar, klines_to_bars
from alpha_core.data.ingest.yahoo import chart_to_bars

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
