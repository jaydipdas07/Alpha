"""NSE F&O bhavcopy transforms -> :class:`OptionQuote` rows (M5.5 options on-ramp).

Pure, network-free converters; the HTTP/zip fetch lives in ``scripts/ingest_nse_fo.py``.
NSE has published the daily derivatives bhavcopy in two formats:

- **legacy** (through 2024-07): ``fo<DD><MON><YYYY>bhav.csv`` — columns ``INSTRUMENT``
  (``OPTIDX``/``OPTSTK``/``FUTIDX``/…), ``SYMBOL``, ``EXPIRY_DT`` (``05-Jan-2023``),
  ``STRIKE_PR``, ``OPTION_TYP`` (``CE``/``PE``), OHLC, ``SETTLE_PR``, ``CONTRACTS``,
  ``OPEN_INT``, ``CHG_IN_OI``, ``TIMESTAMP``.
- **UDiFF** (2024-07 onward): ``BhavCopy_NSE_FO_…csv`` — columns ``FinInstrmTp``
  (``IDO`` index option / ``STO`` stock option / ``IDF``/``STF`` futures), ``TckrSymb``,
  ISO ``TradDt``/``XpryDt``, ``StrkPric``, ``OptnTp``, ``…Pric`` OHLC, ``SttlmPric``,
  ``TtlTradgVol`` (contracts), ``OpnIntrst``, ``ChngInOpnIntrst``, and — a bonus the
  legacy format lacks — ``UndrlygPric``.

Both carry the same economics per contract-day: OHLC + the official **settlement**
mark + open interest. Dates are IST calendar days -> stored as UTC-midnight labels
(the :mod:`~alpha_core.data.options_store` convention). Rows with zero volume are
kept: their settlement price is the exchange's daily mark. ⚠️ On a contract's EXPIRY
day the settlement column instead carries the UNDERLYING's final-settlement level
(see :mod:`~alpha_core.data.options_store` — mark expiring rows at intrinsic/close,
never ``settle``). Money is ``Decimal`` end-to-end (B5).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from alpha_core.core.enums import OptionRight, Venue
from alpha_core.data.options_store import OptionQuote

# Index options only by default: M5.5's research scope is NIFTY/BANKNIFTY chains; stock
# options (OPTSTK/STO) are a later widening, not a silent default.
LEGACY_INDEX_OPTIONS = frozenset({"OPTIDX"})
UDIFF_INDEX_OPTIONS = frozenset({"IDO"})


def _label(day: datetime) -> datetime:
    """An IST calendar day as the store's UTC-midnight label."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


# Explicit month map instead of strptime's %b: %b is LC_TIME-locale-sensitive, so an imported
# library calling setlocale() would break "05-Jan-2023" parsing with a confusing error.
_MONTH_BY_NAME = {
    name: i + 1
    for i, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
    )
}


def _legacy_date(value: str) -> datetime:
    """``05-Jan-2023`` / ``02-JAN-2023`` (any case) -> a UTC-midnight label, locale-proof."""
    day_s, mon_s, year_s = value.strip().split("-")
    month = _MONTH_BY_NAME.get(mon_s.upper())
    if month is None:
        raise ValueError(f"unknown month in legacy date {value!r}")
    return _label(datetime(int(year_s), month, int(day_s), tzinfo=UTC))


def _udiff_date(value: str) -> datetime:
    """ISO ``2026-06-30`` -> a UTC-midnight label."""
    return _label(datetime.strptime(value.strip(), "%Y-%m-%d"))


def _dec(value: str) -> Decimal:
    return Decimal(value.strip())


def _int(value: str) -> int:
    """An integer count that some feeds render with a decimal point (``"360"``/``"360.0"``).
    A genuinely fractional count is malformed data — loud, never silently truncated."""
    d = Decimal(value.strip() or "0")
    if d != d.to_integral_value():
        raise ValueError(f"expected an integer count, got {value!r}")
    return int(d)


def _right(value: str) -> OptionRight:
    """NSE's ``CE``/``PE`` -> the core enum; anything else raises (futures rows are filtered
    upstream by instrument type, so an unknown type here is a malformed row)."""
    v = value.strip()
    if v == "CE":
        return OptionRight.CALL
    if v == "PE":
        return OptionRight.PUT
    raise ValueError(f"unknown option type {value!r} (expected CE/PE)")


def legacy_rows_to_quotes(
    rows: Iterable[Mapping[str, str]],
    *,
    instruments: frozenset[str] = LEGACY_INDEX_OPTIONS,
    underlyings: frozenset[str] | None = None,
) -> list[OptionQuote]:
    """Legacy-format ``csv.DictReader`` rows -> option quotes, keeping only ``instruments``
    (default: index options) and, when given, only ``underlyings``. A malformed row (blank
    strike, unparseable number) raises — the archive is exchange-published and regular, so
    damage means a wrong download, never data to silently skip."""
    out: list[OptionQuote] = []
    rows_seen = 0
    type_column_seen = False
    for row in rows:
        rows_seen += 1
        if "INSTRUMENT" in row:
            type_column_seen = True
        if row.get("INSTRUMENT", "").strip() not in instruments:
            continue
        symbol = row["SYMBOL"].strip()
        if underlyings is not None and symbol not in underlyings:
            continue
        try:
            out.append(
                OptionQuote(
                    underlying=symbol,
                    venue=Venue.NSE,
                    trade_date=_legacy_date(row["TIMESTAMP"]),
                    expiry=_legacy_date(row["EXPIRY_DT"]),
                    strike=_dec(row["STRIKE_PR"]),
                    right=_right(row["OPTION_TYP"]),
                    open=_dec(row["OPEN"]),
                    high=_dec(row["HIGH"]),
                    low=_dec(row["LOW"]),
                    close=_dec(row["CLOSE"]),
                    settle=_dec(row["SETTLE_PR"]),
                    volume_contracts=_int(row["CONTRACTS"]),
                    open_interest=_int(row["OPEN_INT"]),
                    change_in_oi=_int(row["CHG_IN_OI"]),
                )
            )
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise ValueError(f"malformed legacy bhavcopy row {dict(row)!r}") from exc
    if rows_seen and not type_column_seen:
        # a whole file without the type column is a wrong download / format drift — loud,
        # never a silent 0-row trading day (per-row .get tolerates only mixed rows).
        raise ValueError("legacy bhavcopy file has no INSTRUMENT column — wrong/drifted format")
    return out


def udiff_rows_to_quotes(
    rows: Iterable[Mapping[str, str]],
    *,
    instruments: frozenset[str] = UDIFF_INDEX_OPTIONS,
    underlyings: frozenset[str] | None = None,
) -> list[OptionQuote]:
    """UDiFF-format ``csv.DictReader`` rows -> option quotes (same filtering contract as
    :func:`legacy_rows_to_quotes`); carries the ``UndrlygPric`` the legacy format lacks."""
    out: list[OptionQuote] = []
    rows_seen = 0
    type_column_seen = False
    for row in rows:
        rows_seen += 1
        if "FinInstrmTp" in row:
            type_column_seen = True
        if row.get("FinInstrmTp", "").strip() not in instruments:
            continue
        symbol = row["TckrSymb"].strip()
        if underlyings is not None and symbol not in underlyings:
            continue
        try:
            underlying_close = row.get("UndrlygPric", "").strip()
            out.append(
                OptionQuote(
                    underlying=symbol,
                    venue=Venue.NSE,
                    trade_date=_udiff_date(row["TradDt"]),
                    expiry=_udiff_date(row["XpryDt"]),
                    strike=_dec(row["StrkPric"]),
                    right=_right(row["OptnTp"]),
                    open=_dec(row["OpnPric"]),
                    high=_dec(row["HghPric"]),
                    low=_dec(row["LwPric"]),
                    close=_dec(row["ClsPric"]),
                    settle=_dec(row["SttlmPric"]),
                    volume_contracts=_int(row["TtlTradgVol"]),
                    open_interest=_int(row["OpnIntrst"]),
                    change_in_oi=_int(row["ChngInOpnIntrst"]),
                    underlying_close=_dec(underlying_close) if underlying_close else None,
                )
            )
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise ValueError(f"malformed UDiFF bhavcopy row {dict(row)!r}") from exc
    if rows_seen and not type_column_seen:
        raise ValueError("UDiFF bhavcopy file has no FinInstrmTp column — wrong/drifted format")
    return out
