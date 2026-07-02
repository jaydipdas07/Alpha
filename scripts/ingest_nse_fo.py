"""NSE F&O bhavcopy ingest — a decade of free EOD index-option chains (M5.5 on-ramp).

Downloads the daily derivatives bhavcopy over a date window and stores the index-option
rows for the chosen underlyings in the Parquet :class:`OptionsStore`. Two archive
formats, both free (probed live 2026-07-02: UDiFF serves 2024-07→today, legacy at least
2016→2024-06):

- UDiFF:  ``https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip``
- legacy: ``https://archives.nseindia.com/content/historical/DERIVATIVES/<YYYY>/<MON>/fo<DD><MON><YYYY>bhav.csv.zip``

Per date the era-appropriate format is tried first with the other as fallback; a 404 on
both is a holiday/weekend (skipped, counted). Writes are batched monthly (the store
merges idempotently per (venue, underlying, year) file — re-running a window never
duplicates). Fixed default window end (never "now") for reproducibility.

Mac-CLI / network — not a CI test (the pure transforms in ``data/ingest/nse_fo_bhavcopy``
are the tested part). The store root is the repo-gitignored ``data_options/``,
overridable via ``ALPHA_OPTIONS_ROOT``. Run::

    uv run python scripts/ingest_nse_fo.py --start 2016-01-01 --end 2026-06-30
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, timedelta
from pathlib import Path

from alpha_core.data.ingest.nse_fo_bhavcopy import (
    legacy_rows_to_quotes,
    udiff_rows_to_quotes,
)
from alpha_core.data.options_store import OptionQuote, OptionsStore

STORE_ROOT = Path(
    os.environ.get("ALPHA_OPTIONS_ROOT") or (Path(__file__).resolve().parents[1] / "data_options")
)
_HEADERS = {"User-Agent": "Mozilla/5.0 (alpha-research NSE bhavcopy ingest)"}

# NSE switched the published format to UDiFF in July 2024; each side falls back to the
# other, so the cutover only orders the attempts (a wrong guess costs one 404).
_UDIFF_FROM = date(2024, 7, 1)
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _udiff_url(day: date) -> str:
    return (
        "https://nsearchives.nseindia.com/content/fo/"
        f"BhavCopy_NSE_FO_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
    )


def _legacy_url(day: date) -> str:
    mon = _MONTHS[day.month - 1]
    return (
        "https://archives.nseindia.com/content/historical/DERIVATIVES/"
        f"{day.year}/{mon}/fo{day:%d}{mon}{day.year}bhav.csv.zip"
    )


def _fetch_csv(url: str) -> list[dict[str, str]] | None:
    """The zipped bhavcopy's rows as DictReader dicts, or ``None`` on 404 (holiday /
    format-not-published-for-this-date). Other HTTP errors raise (a wrong URL or a block
    must be loud, not an empty day)."""
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        member = archive.namelist()[0]  # each bhavcopy zip holds exactly one CSV
        with archive.open(member) as raw:
            return list(csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8")))


def _quotes_for(day: date, underlyings: frozenset[str]) -> list[OptionQuote] | None:
    """One trading day's index-option quotes, or ``None`` when neither format has the day
    (holiday). The era-appropriate format is tried first; each parses with its own transform."""
    attempts = (
        (("udiff", _udiff_url(day)), ("legacy", _legacy_url(day)))
        if day >= _UDIFF_FROM
        else (("legacy", _legacy_url(day)), ("udiff", _udiff_url(day)))
    )
    for fmt, url in attempts:
        rows = _fetch_csv(url)
        if rows is None:
            continue
        if fmt == "udiff":
            return udiff_rows_to_quotes(rows, underlyings=underlyings)
        return legacy_rows_to_quotes(rows, underlyings=underlyings)
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest NSE F&O bhavcopy index-option chains")
    ap.add_argument("--start", default="2016-01-01", help="first trading date (ISO)")
    ap.add_argument("--end", default="2026-06-30", help="last trading date (ISO, inclusive)")
    ap.add_argument(
        "--underlyings",
        default="NIFTY,BANKNIFTY",
        help="comma-separated index-option underlyings to keep",
    )
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    underlyings = frozenset(u.strip() for u in args.underlyings.split(",") if u.strip())
    store = OptionsStore(STORE_ROOT)
    print(f"=== NSE F&O ingest {start} .. {end} ({', '.join(sorted(underlyings))}) ===")

    batch: list[OptionQuote] = []
    month: tuple[int, int] | None = None
    days = skipped = total = 0
    day = start
    while day <= end:
        if day.weekday() < 5:  # NSE trades Mon-Fri; holidays 404 and are counted below
            if month is not None and (day.year, day.month) != month and batch:
                store.write(batch)  # monthly batches: idempotent merges per year file
                print(f"[{month[0]}-{month[1]:02d}] {len(batch)} rows stored")
                batch = []
            month = (day.year, day.month)
            quotes = _quotes_for(day, underlyings)
            if quotes is None:
                skipped += 1
            else:
                batch.extend(quotes)
                days += 1
                total += len(quotes)
            time.sleep(0.3)  # polite to the public archive
        day += timedelta(days=1)
    if batch and month is not None:
        store.write(batch)
        print(f"[{month[0]}-{month[1]:02d}] {len(batch)} rows stored")

    con = store.connect()
    rows = con.execute(
        "SELECT underlying, count(*) n, min(trade_date) lo, max(trade_date) hi "
        "FROM option_quotes GROUP BY 1 ORDER BY 1"
    ).fetchall()
    for underlying, n, lo, hi in rows:
        print(f"[duckdb]  {underlying}: {n} contract-days  {lo} .. {hi}")
    print(
        f"=== done — {total} rows over {days} trading days ({skipped} holidays/gaps skipped), "
        f"reproducible (idempotent re-write) -> {STORE_ROOT} ==="
    )


if __name__ == "__main__":
    main()
