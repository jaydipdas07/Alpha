"""TickStore — month partitioning, idempotent merge, columnar reads (F3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.tick_store import TickStore, month_of

_T0 = datetime(2025, 1, 31, 23, 59, 58, tzinfo=UTC)


def _bar(i: int, close: str = "100") -> Bar:
    c = Decimal(close)
    return Bar(
        symbol="BTCUSDT",
        venue=Venue.BINANCE,
        asset_class=AssetClass.CRYPTO,
        start=_T0 + timedelta(seconds=i),
        interval=timedelta(seconds=1),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=Decimal("1"),
    )


def test_month_of_requires_tz_aware() -> None:
    assert month_of(_T0) == "2025-01"
    with pytest.raises(ValueError):
        month_of(datetime(2025, 1, 1))


def test_write_read_roundtrip_and_partitioning(tmp_path: Path) -> None:
    store = TickStore(tmp_path)
    jan = [_bar(0), _bar(1)]  # 23:59:58, 23:59:59 on Jan 31
    feb = [_bar(2), _bar(3)]  # 00:00:00, 00:00:01 on Feb 1
    assert store.write_bars(jan, month="2025-01") == 2
    assert store.write_bars(feb, month="2025-02") == 2
    assert store.months(Venue.BINANCE, "BTCUSDT", 1) == ["2025-01", "2025-02"]
    table = store.read_columns(venue=Venue.BINANCE, symbol="BTCUSDT", interval_seconds=1)
    starts = table.column("start").to_pylist()
    assert [s.astimezone(UTC) for s in starts] == [b.start for b in jan + feb]  # ordered
    span = store.span(Venue.BINANCE, "BTCUSDT", 1)
    assert span == (jan[0].start, feb[-1].start)


def test_write_is_idempotent_and_dedups_by_start(tmp_path: Path) -> None:
    store = TickStore(tmp_path)
    assert store.write_bars([_bar(0, "100")], month="2025-01") == 1
    # same start re-written with a new close: newest wins, count stays 1
    assert store.write_bars([_bar(0, "101")], month="2025-01") == 1
    table = store.read_columns(
        venue=Venue.BINANCE, symbol="BTCUSDT", interval_seconds=1, columns=("start", "close")
    )
    assert table.num_rows == 1
    assert Decimal(str(table.column("close")[0].as_py())) == Decimal("101")


def test_write_rejects_wrong_month_and_mixed_series(tmp_path: Path) -> None:
    store = TickStore(tmp_path)
    with pytest.raises(ValueError, match="outside partition month"):
        store.write_bars([_bar(2)], month="2025-01")  # Feb bar into the Jan partition
    other = _bar(0).model_copy(update={"symbol": "ETHUSDT"})
    with pytest.raises(ValueError, match="one series"):
        store.write_bars([_bar(0), other], month="2025-01")
    with pytest.raises(ValueError, match="YYYY-MM"):
        store.write_bars([_bar(0)], month="2025-1")


def test_read_columns_range_filter_and_empty_series(tmp_path: Path) -> None:
    store = TickStore(tmp_path)
    store.write_bars([_bar(0), _bar(1)], month="2025-01")
    store.write_bars([_bar(2), _bar(3)], month="2025-02")
    table = store.read_columns(
        venue=Venue.BINANCE,
        symbol="BTCUSDT",
        interval_seconds=1,
        start=_T0 + timedelta(seconds=1),
        end=_T0 + timedelta(seconds=3),
    )
    assert table.num_rows == 2  # [start, end)
    empty = store.read_columns(venue=Venue.BINANCE, symbol="NOPE", interval_seconds=1)
    assert empty.num_rows == 0
    assert store.span(Venue.BINANCE, "NOPE", 1) is None


def test_month_spans_maps_each_partition(tmp_path: Path) -> None:
    store = TickStore(tmp_path)
    store.write_bars([_bar(0), _bar(1)], month="2025-01")
    store.write_bars([_bar(2), _bar(3)], month="2025-02")
    spans = store.month_spans(Venue.BINANCE, "BTCUSDT", 1)
    assert set(spans) == {"2025-01", "2025-02"}
    assert spans["2025-01"] == (_bar(0).start, _bar(1).start)
    assert spans["2025-02"] == (_bar(2).start, _bar(3).start)
    assert store.month_spans(Venue.BINANCE, "NOPE", 1) == {}
