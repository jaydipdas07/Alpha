"""The M5.5 options data layer — both bhavcopy transforms (real-shaped rows sampled from the
live archives on 2026-07-02) and the OptionsStore round-trip/dedup. Money must come back
``Decimal`` and dates as UTC-midnight labels (B5 / the store's label convention)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from alpha_core.core.enums import OptionRight, Venue
from alpha_core.data.ingest.nse_fo_bhavcopy import (
    legacy_rows_to_quotes,
    udiff_rows_to_quotes,
)
from alpha_core.data.options_store import OptionQuote, OptionsStore

# A real legacy row (fo02JAN2023bhav.csv) and its shape-mates for filter tests.
_LEGACY_NIFTY = {
    "INSTRUMENT": "OPTIDX",
    "SYMBOL": "NIFTY",
    "EXPIRY_DT": "05-Jan-2023",
    "STRIKE_PR": "15050",
    "OPTION_TYP": "CE",
    "OPEN": "0",
    "HIGH": "0",
    "LOW": "0",
    "CLOSE": "3176.95",
    "SETTLE_PR": "3155.8",
    "CONTRACTS": "0",
    "VAL_INLAKH": "0",
    "OPEN_INT": "0",
    "CHG_IN_OI": "0",
    "TIMESTAMP": "02-JAN-2023",  # upper-case month: strptime's %b is case-insensitive
}
_LEGACY_FUT = dict(_LEGACY_NIFTY, INSTRUMENT="FUTIDX", OPTION_TYP="XX")
_LEGACY_STOCK_OPT = dict(_LEGACY_NIFTY, INSTRUMENT="OPTSTK", SYMBOL="RELIANCE")

# A real UDiFF row (BhavCopy_NSE_FO_..._20260630) trimmed to the consumed columns.
_UDIFF_NIFTY = {
    "TradDt": "2026-06-30",
    "FinInstrmTp": "IDO",
    "TckrSymb": "NIFTY",
    "XpryDt": "2026-07-28",
    "StrkPric": "22850.00",
    "OptnTp": "PE",
    "OpnPric": "52.40",
    "HghPric": "62.75",
    "LwPric": "43.55",
    "ClsPric": "52.15",
    "UndrlygPric": "23865.75",
    "SttlmPric": "52.15",
    "OpnIntrst": "36205",
    "ChngInOpnIntrst": "2535",
    "TtlTradgVol": "360",
}
_UDIFF_STOCK_OPT = dict(_UDIFF_NIFTY, FinInstrmTp="STO", TckrSymb="ABCAPITAL")


def test_legacy_row_parses_decimal_utc_and_right() -> None:
    (q,) = legacy_rows_to_quotes([_LEGACY_NIFTY, _LEGACY_FUT])  # the futures row is filtered
    assert q.underlying == "NIFTY" and q.venue is Venue.NSE
    assert q.trade_date == datetime(2023, 1, 2, tzinfo=UTC)  # UTC-midnight label
    assert q.expiry == datetime(2023, 1, 5, tzinfo=UTC)
    assert q.right is OptionRight.CALL
    assert q.strike == Decimal("15050") and isinstance(q.strike, Decimal)
    assert q.settle == Decimal("3155.8") and isinstance(q.settle, Decimal)
    assert (q.volume_contracts, q.open_interest, q.change_in_oi) == (0, 0, 0)
    assert q.underlying_close is None  # the legacy format does not carry it


def test_udiff_row_parses_underlying_close_and_put() -> None:
    (q,) = udiff_rows_to_quotes([_UDIFF_NIFTY, _UDIFF_STOCK_OPT])  # STO filtered by default
    assert q.right is OptionRight.PUT
    assert q.trade_date == datetime(2026, 6, 30, tzinfo=UTC)
    assert q.expiry == datetime(2026, 7, 28, tzinfo=UTC)
    assert q.strike == Decimal("22850.00")
    assert q.underlying_close == Decimal("23865.75")
    assert q.volume_contracts == 360 and q.open_interest == 36205


def test_underlyings_filter_keeps_only_the_requested_names() -> None:
    banknifty = dict(_UDIFF_NIFTY, TckrSymb="BANKNIFTY")
    quotes = udiff_rows_to_quotes([_UDIFF_NIFTY, banknifty], underlyings=frozenset({"BANKNIFTY"}))
    assert [q.underlying for q in quotes] == ["BANKNIFTY"]
    legacy = legacy_rows_to_quotes(
        [_LEGACY_NIFTY, _LEGACY_STOCK_OPT],
        instruments=frozenset({"OPTIDX", "OPTSTK"}),
        underlyings=frozenset({"NIFTY"}),
    )
    assert [q.underlying for q in legacy] == ["NIFTY"]


def test_blank_udiff_underlying_price_becomes_none() -> None:
    row = dict(_UDIFF_NIFTY, UndrlygPric="")
    (q,) = udiff_rows_to_quotes([row])
    assert q.underlying_close is None


def test_malformed_rows_raise_with_context() -> None:
    with pytest.raises(ValueError, match="malformed legacy bhavcopy row"):
        legacy_rows_to_quotes([dict(_LEGACY_NIFTY, STRIKE_PR="not-a-number")])
    with pytest.raises(ValueError, match="malformed UDiFF bhavcopy row"):
        udiff_rows_to_quotes([dict(_UDIFF_NIFTY, OptnTp="XX")])  # not CE/PE -> loud


# --- the store: round-trip, dedup, year files, window reads ---------------------------------------


def _quote(day: int, strike: str = "22000", month: int = 1, year: int = 2026) -> OptionQuote:
    return OptionQuote(
        underlying="NIFTY",
        venue=Venue.NSE,
        trade_date=datetime(year, month, day, tzinfo=UTC),
        expiry=datetime(year, month, 28, tzinfo=UTC),
        strike=Decimal(strike),
        right=OptionRight.CALL,
        open=Decimal("10"),
        high=Decimal("12"),
        low=Decimal("9"),
        close=Decimal("11"),
        settle=Decimal("11.5"),
        volume_contracts=5,
        open_interest=100,
        change_in_oi=-3,
    )


def test_store_round_trip_sorted_and_decimal(tmp_path: Path) -> None:
    store = OptionsStore(tmp_path)
    store.write([_quote(3), _quote(2), _quote(2, strike="21000")])
    quotes = store.read(underlying="NIFTY", venue=Venue.NSE)
    # Decimal VALUE equality: parquet decimal128 scale-normalizes ("21000" -> "21000.000...").
    assert [(q.trade_date.day, q.strike) for q in quotes] == [
        (2, Decimal("21000")),
        (2, Decimal("22000")),
        (3, Decimal("22000")),
    ]
    assert isinstance(quotes[0].settle, Decimal) and quotes[0].settle == Decimal("11.5")


def test_store_write_is_idempotent_and_newest_wins(tmp_path: Path) -> None:
    store = OptionsStore(tmp_path)
    assert store.write([_quote(2)]) == 1
    assert store.write([_quote(2)]) == 1  # same contract-day: a no-op, never a duplicate
    revised = _quote(2).model_copy(update={"settle": Decimal("99")})
    assert store.write([revised]) == 1
    (q,) = store.read(underlying="NIFTY", venue=Venue.NSE)
    assert q.settle == Decimal("99")  # newest wins on the dedup key


def test_store_splits_files_by_year_and_reads_across_them(tmp_path: Path) -> None:
    store = OptionsStore(tmp_path)
    store.write([_quote(2, year=2025), _quote(2, year=2026)])
    files = sorted(p.name for p in tmp_path.glob("*.parquet"))
    assert files == ["NSE__NIFTY__2025.parquet", "NSE__NIFTY__2026.parquet"]
    assert len(store.read(underlying="NIFTY", venue=Venue.NSE)) == 2
    windowed = store.read(
        underlying="NIFTY",
        venue=Venue.NSE,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2027, 1, 1, tzinfo=UTC),
    )
    assert [q.trade_date.year for q in windowed] == [2026]


def test_store_duckdb_view_counts_and_is_empty_safe(tmp_path: Path) -> None:
    empty = OptionsStore(tmp_path / "empty").connect()
    assert empty.execute("SELECT count(*) FROM option_quotes").fetchone() == (0,)
    store = OptionsStore(tmp_path / "full")
    store.write([_quote(2), _quote(3)])
    con = store.connect()
    assert con.execute("SELECT count(*) FROM option_quotes").fetchone() == (2,)
