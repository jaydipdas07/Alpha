"""The funding-rate **data layer** (M3.0): the Binance funding transform + the Parquet FundingStore.

Distinct from ``test_funding.py`` (which covers ``execution.funding`` — the live funding *accrual*
on a held position, R13). This is the historical funding-rate *data feed* for the carry signal:
it guards the money invariant (signed Decimal, never a float), tz-aware UTC times, and the store's
idempotent dedup-by-time — the same guarantees BarStore gives, for funding."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from alpha_core.core.enums import Venue
from alpha_core.data.funding import FundingRate, FundingStore
from alpha_core.data.ingest.binance_funding import funding_rows_to_rates

T0 = datetime(2023, 6, 1, tzinfo=UTC)
H8 = timedelta(hours=8)  # Binance funds every 8 hours


def _rate(symbol: str, when: datetime, rate: str) -> FundingRate:
    return FundingRate(symbol=symbol, venue=Venue.BINANCE, funding_time=when, rate=Decimal(rate))


# --- the pure Binance transform ------------------------------------------------------------------


def test_funding_rows_to_rates_parses_signed_decimal_and_utc() -> None:
    rows = [
        {"symbol": "BTCUSDT", "fundingTime": 1685577600000, "fundingRate": "0.00002096"},
        {"symbol": "BTCUSDT", "fundingTime": 1685606400000, "fundingRate": "-0.00010000"},
    ]
    rates = funding_rows_to_rates(rows, symbol="BTCUSDT")
    assert [r.symbol for r in rates] == ["BTCUSDT", "BTCUSDT"]
    assert rates[0].venue is Venue.BINANCE
    assert rates[0].funding_time == T0  # 1685577600000 ms == 2023-06-01 00:00 UTC
    assert rates[0].funding_time.tzinfo is not None
    assert rates[0].rate == Decimal("0.00002096")
    assert rates[1].rate == Decimal(
        "-0.00010000"
    )  # a negative rate (shorts pay longs) is preserved


def test_funding_rows_empty_page() -> None:
    assert funding_rows_to_rates([], symbol="BTCUSDT") == []


def test_funding_rate_rejects_a_float() -> None:
    # the money invariant: a float rate is rejected (the _no_float BeforeValidator on Money).
    with pytest.raises(ValidationError):
        FundingRate(symbol="X", venue=Venue.BINANCE, funding_time=T0, rate=0.001)  # type: ignore[arg-type]


# --- the Parquet store ---------------------------------------------------------------------------


def test_store_write_read_roundtrip_preserves_signed_decimal(tmp_path: Path) -> None:
    store = FundingStore(tmp_path)
    store.write([_rate("BTCUSDT", T0, "0.0001"), _rate("BTCUSDT", T0 + H8, "-0.0002")])
    got = store.read(symbol="BTCUSDT", venue=Venue.BINANCE)
    assert [r.funding_time for r in got] == [T0, T0 + H8]  # sorted by time
    assert [r.rate for r in got] == [
        Decimal("0.0001"),
        Decimal("-0.0002"),
    ]  # Decimal, signed, exact


def test_store_dedup_by_time_is_idempotent_newest_wins(tmp_path: Path) -> None:
    store = FundingStore(tmp_path)
    assert store.write([_rate("BTCUSDT", T0, "0.0001")]) == 1
    assert store.write([_rate("BTCUSDT", T0, "0.0001")]) == 1  # re-write same -> no duplicate
    assert store.write([_rate("BTCUSDT", T0, "0.0009")]) == 1  # same time, newest wins
    got = store.read(symbol="BTCUSDT", venue=Venue.BINANCE)
    assert len(got) == 1 and got[0].rate == Decimal("0.0009")


def test_store_read_window_is_half_open(tmp_path: Path) -> None:
    store = FundingStore(tmp_path)
    store.write(
        [
            _rate("BTCUSDT", T0, "0.1"),
            _rate("BTCUSDT", T0 + H8, "0.2"),
            _rate("BTCUSDT", T0 + 2 * H8, "0.3"),
        ]
    )
    got = store.read(symbol="BTCUSDT", venue=Venue.BINANCE, start=T0 + H8, end=T0 + 2 * H8)
    assert [r.rate for r in got] == [Decimal("0.2")]  # [start, end)


def test_store_missing_series_reads_empty(tmp_path: Path) -> None:
    store = FundingStore(tmp_path)
    assert store.root == tmp_path
    assert store.read(symbol="NOPE", venue=Venue.BINANCE) == []


def test_store_separates_symbols(tmp_path: Path) -> None:
    store = FundingStore(tmp_path)
    store.write([_rate("BTCUSDT", T0, "0.1"), _rate("ETHUSDT", T0, "0.2")])
    assert store.read(symbol="BTCUSDT", venue=Venue.BINANCE)[0].rate == Decimal("0.1")
    assert store.read(symbol="ETHUSDT", venue=Venue.BINANCE)[0].rate == Decimal("0.2")


def test_store_connect_queries_every_series(tmp_path: Path) -> None:
    store = FundingStore(tmp_path)
    store.write(
        [_rate("BTCUSDT", T0, "0.1"), _rate("BTCUSDT", T0 + H8, "0.2"), _rate("ETHUSDT", T0, "0.3")]
    )
    row = store.connect().execute("SELECT count(*), count(DISTINCT symbol) FROM funding").fetchone()
    assert row is not None and row[0] == 3 and row[1] == 2


def test_store_connect_empty_is_safe(tmp_path: Path) -> None:
    row = FundingStore(tmp_path).connect().execute("SELECT count(*) FROM funding").fetchone()
    assert row is not None and row[0] == 0
