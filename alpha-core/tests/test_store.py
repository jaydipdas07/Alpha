"""BarStore Parquet/DuckDB cold-store tests (B1a.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.store import BarStore

SYM = "BTC/USDT:USDT"  # a crypto symbol with / and : — exercises the path-safe key
START = datetime(2026, 6, 26, tzinfo=UTC)
FIVE_MIN = timedelta(minutes=5)


def _bars(closes: list[str]) -> list[Bar]:
    return [
        Bar(
            symbol=SYM,
            venue=Venue.BINANCE,
            asset_class=AssetClass.CRYPTO,
            start=START + i * FIVE_MIN,
            interval=FIVE_MIN,
            open=Decimal(c),
            high=Decimal(c) + 10,
            low=Decimal(c) - 10,
            close=Decimal(c),
            volume=Decimal("1.5"),
        )
        for i, c in enumerate(closes)
    ]


def test_round_trip_is_decimal_exact(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    bars = _bars(["30000.12345678", "30100.5", "30200"])
    assert store.write_bars(bars) == 3
    got = store.read_bars(symbol=SYM, venue=Venue.BINANCE, interval_seconds=300)
    assert got == bars  # Decimal-exact (value), tz-aware UTC, sorted by start


def test_rewrite_is_idempotent(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    bars = _bars(["30000", "30100", "30200"])
    store.write_bars(bars)
    store.write_bars(bars)  # re-ingest the same window -> reproducible, no duplicates
    got = store.read_bars(symbol=SYM, venue=Venue.BINANCE, interval_seconds=300)
    assert got == bars


def test_dedup_newest_wins(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    store.write_bars(_bars(["30000"]))
    revised = _bars(["30000"])[0].model_copy(update={"close": Decimal("30005")})  # valid OHLC
    store.write_bars([revised])  # same start -> overwrites, not appends
    got = store.read_bars(symbol=SYM, venue=Venue.BINANCE, interval_seconds=300)
    assert len(got) == 1 and got[0].close == Decimal("30005")


def test_time_window_is_half_open(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    store.write_bars(_bars(["30001", "30002", "30003", "30004", "30005"]))
    got = store.read_bars(
        symbol=SYM,
        venue=Venue.BINANCE,
        interval_seconds=300,
        start=START + FIVE_MIN,
        end=START + 4 * FIVE_MIN,
    )
    assert [b.close for b in got] == [Decimal("30002"), Decimal("30003"), Decimal("30004")]


def test_missing_series_reads_empty(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    assert store.read_bars(symbol="ETH/USDT", venue=Venue.BINANCE, interval_seconds=300) == []


def test_duckdb_query_over_the_cold_store(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    store.write_bars(_bars(["30000", "30100", "30200"]))
    con = store.connect()
    row = con.execute("SELECT count(*), avg(close) FROM bars").fetchone()
    assert row is not None
    count, avg = row
    assert count == 3
    assert abs(float(avg) - 30100) < 1e-9  # the DuckDB analytical layer reads the Parquet
