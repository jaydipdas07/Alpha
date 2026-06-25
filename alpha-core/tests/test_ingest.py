"""Ingest transform tests (B1a.1b) — pure row/JSON -> Bar, no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.data.ingest.binance import kline_to_bar, klines_to_bars
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
