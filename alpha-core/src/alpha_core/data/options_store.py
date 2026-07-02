"""Indian index-option EOD chain history (M5.5 — the options research on-ramp).

The free NSE F&O **bhavcopy** archive publishes, for every listed contract, the day's
OHLC + **settlement price** + open interest — a decade of daily option chains at zero
cost, in the market DESIGN_v4 names the real edge target (E4). This module is the
**data layer**: a typed :class:`OptionQuote` record + a Parquet :class:`OptionsStore`
mirroring :class:`~alpha_core.data.funding.FundingStore` (idempotent dedup,
DuckDB-queryable, native DECIMAL). Research-plane only (Parquet/DuckDB are dev-group
deps; the lean worker never imports it).

Invariants: every price/strike is ``Decimal`` (B5 — never a float); dates are tz-aware
UTC **labels** — NSE trading/expiry dates are IST calendar days, stored uniformly as
that calendar date at 00:00 UTC (a label, not an instant; both bhavcopy formats carry
dates only, and research compares labels). A row with zero volume is still a valid
record: its settlement price is the exchange's official daily mark (deep strikes trade
rarely but settle daily).

⚠️ **Expiry-day ``settle`` is NOT the option's mark.** On a contract's expiry day NSE
publishes the UNDERLYING's final-settlement level in the settlement column (verified
live 2026-06-30: every expiring row's ``SttlmPric`` equals ``UndrlygPric``; no
non-expiring row's does). The store persists what the exchange publishes; a fold must
mark rows with ``trade_date == expiry`` at intrinsic value (or ``close``), never at
``settle`` — else every expiry day corrupts P&L by the full index level.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field

from alpha_core.core.enums import OptionRight, Venue
from alpha_core.core.models import Money, NonNegMoney, PosMoney, UtcDatetime
from alpha_core.data.holdout import (
    HoldoutWindow,
    assert_disjoint_roots,
    compute_holdout_window,
    floored_window,
    prior_window_starts,
    write_seal_manifest,
)


class OptionQuote(BaseModel):
    """One contract's EOD row: the ``(underlying, expiry, strike, right)`` contract's
    prices, settlement, and open interest on ``trade_date``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    underlying: str  # e.g. "NIFTY", "BANKNIFTY"
    venue: Venue
    trade_date: UtcDatetime  # the IST trading day as a UTC-midnight label
    expiry: UtcDatetime  # the contract's expiry day, same label convention
    strike: PosMoney
    right: OptionRight
    open: NonNegMoney  # 0 when the contract did not trade that day
    high: NonNegMoney
    low: NonNegMoney
    close: NonNegMoney
    # the official daily mark (present even with zero volume) — EXCEPT on expiry day, when
    # NSE publishes the UNDERLYING's final-settlement level here (mark expiring rows at
    # intrinsic/close, never settle; see the module docstring).
    settle: NonNegMoney
    volume_contracts: int = Field(ge=0)  # contracts traded
    open_interest: int = Field(ge=0)
    change_in_oi: int
    underlying_close: Money | None = None  # UDiFF carries it; legacy rows leave it None


_PRICE = pa.decimal128(38, 18)  # native DECIMAL, scale-normalized — never a float (B5)
_SCHEMA = pa.schema(
    [
        ("underlying", pa.string()),
        ("venue", pa.string()),
        ("trade_date", pa.timestamp("us", tz="UTC")),
        ("expiry", pa.timestamp("us", tz="UTC")),
        ("strike", _PRICE),
        ("right", pa.string()),
        ("open", _PRICE),
        ("high", _PRICE),
        ("low", _PRICE),
        ("close", _PRICE),
        ("settle", _PRICE),
        ("volume_contracts", pa.int64()),
        ("open_interest", pa.int64()),
        ("change_in_oi", pa.int64()),
        ("underlying_close", _PRICE),
    ]
)

# One contract-day is unique by this key (the dedup identity within an (underlying, year) file).
_KEY = ("trade_date", "expiry", "strike", "right")


def _safe(value: str) -> str:
    """A reversible, filesystem-safe key segment (percent-encode, like ``BarStore._safe``)."""
    return quote(value, safe="")


def _row(q: OptionQuote) -> dict[str, Any]:
    return {
        "underlying": q.underlying,
        "venue": q.venue.value,
        "trade_date": q.trade_date,
        "expiry": q.expiry,
        "strike": q.strike,
        "right": q.right.value,
        "open": q.open,
        "high": q.high,
        "low": q.low,
        "close": q.close,
        "settle": q.settle,
        "volume_contracts": q.volume_contracts,
        "open_interest": q.open_interest,
        "change_in_oi": q.change_in_oi,
        "underlying_close": q.underlying_close,
    }


def _quote(row: dict[str, Any]) -> OptionQuote:
    return OptionQuote(
        underlying=str(row["underlying"]),
        venue=Venue(row["venue"]),
        trade_date=row["trade_date"],
        expiry=row["expiry"],
        strike=row["strike"],
        right=OptionRight(row["right"]),
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        settle=row["settle"],
        volume_contracts=int(row["volume_contracts"]),
        open_interest=int(row["open_interest"]),
        change_in_oi=int(row["change_in_oi"]),
        underlying_close=row["underlying_close"],
    )


class OptionsStore:
    """A Parquet store of EOD option quotes under ``root`` — one file per
    ``(venue, underlying, trade-year)`` (a NIFTY year is ~400k rows; a decade of two
    underlyings stays a handful of compact files)."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """The store's on-disk root directory (read-only)."""
        return self._root

    def series(self) -> list[tuple[str, Venue]]:
        """The distinct ``(underlying, venue)`` series on disk (parsed from the year-file
        names), sorted — the seal iterates these."""
        found: set[tuple[str, Venue]] = set()
        for path in self._root.glob("*.parquet"):
            venue_s, underlying_s, _year = path.stem.split("__", 2)
            found.add((unquote(underlying_s), Venue(venue_s)))
        return sorted(found, key=lambda s: (s[1].value, s[0]))

    def _path(self, venue: Venue, underlying: str, year: int) -> Path:
        return self._root / f"{venue.value}__{_safe(underlying)}__{year}.parquet"

    def write(self, quotes: Sequence[OptionQuote]) -> int:
        """Persist ``quotes`` (idempotent — dedup by the contract-day key; newest wins).
        Returns the number of distinct rows on disk for the touched files after the merge;
        re-writing the same quotes is a no-op."""
        groups: dict[tuple[Venue, str, int], list[OptionQuote]] = {}
        for q in quotes:
            groups.setdefault((q.venue, q.underlying, q.trade_date.year), []).append(q)

        written = 0
        for (venue, underlying, year), group in groups.items():
            path = self._path(venue, underlying, year)
            merged: dict[tuple[datetime, datetime, Decimal, str], OptionQuote] = {}
            if path.exists():
                for existing in self._read_file(path):
                    merged[self._key(existing)] = existing
            for q in group:
                merged[self._key(q)] = q  # newest wins — dedup by contract-day
            ordered = sorted(
                merged.values(), key=lambda q: (q.trade_date, q.expiry, q.strike, q.right.value)
            )
            pq.write_table(pa.Table.from_pylist([_row(q) for q in ordered], schema=_SCHEMA), path)
            written += len(ordered)
        return written

    @staticmethod
    def _key(q: OptionQuote) -> tuple[datetime, datetime, Decimal, str]:
        # the strike keys by Decimal VALUE (hash/eq ignore scale): parquet decimal128(38,18)
        # normalizes "22000" to "22000.000...", so a string key would defeat the dedup.
        return (q.trade_date, q.expiry, q.strike, q.right.value)

    def read(
        self,
        *,
        underlying: str,
        venue: Venue,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[OptionQuote]:
        """Read one underlying's quotes over ``[start, end)`` (by ``trade_date``), sorted by
        (trade_date, expiry, strike, right). Spans year files transparently."""
        quotes: list[OptionQuote] = []
        for path in sorted(self._root.glob(f"{venue.value}__{_safe(underlying)}__*.parquet")):
            quotes.extend(self._read_file(path))
        if start is not None:
            quotes = [q for q in quotes if q.trade_date >= start]
        if end is not None:
            quotes = [q for q in quotes if q.trade_date < end]
        return sorted(quotes, key=lambda q: (q.trade_date, q.expiry, q.strike, q.right.value))

    def _read_file(self, path: Path) -> list[OptionQuote]:
        rows: list[dict[str, Any]] = pq.read_table(path).to_pylist()
        return [_quote(r) for r in rows]

    def connect(self) -> duckdb.DuckDBPyConnection:
        """A DuckDB connection with an ``option_quotes`` view over every parquet (empty-safe).
        NB ``right`` is a SQL keyword: quote it in ad-hoc queries (``SELECT "right" …``);
        ``SELECT *`` and the view itself need no quoting."""
        con = duckdb.connect()
        files = sorted(self._root.glob("*.parquet"))
        if files:
            globbed = str(self._root / "*.parquet").replace("'", "''")
            con.execute(f"CREATE VIEW option_quotes AS SELECT * FROM read_parquet('{globbed}')")
        else:
            con.execute(
                "CREATE VIEW option_quotes AS SELECT NULL::VARCHAR AS underlying, "
                "NULL::VARCHAR AS venue, NULL::TIMESTAMPTZ AS trade_date, "
                "NULL::TIMESTAMPTZ AS expiry, NULL::DECIMAL(38,18) AS strike, "
                "NULL::VARCHAR AS right, NULL::DECIMAL(38,18) AS open, "
                "NULL::DECIMAL(38,18) AS high, NULL::DECIMAL(38,18) AS low, "
                "NULL::DECIMAL(38,18) AS close, NULL::DECIMAL(38,18) AS settle, "
                "NULL::BIGINT AS volume_contracts, NULL::BIGINT AS open_interest, "
                "NULL::BIGINT AS change_in_oi, NULL::DECIMAL(38,18) AS underlying_close "
                "WHERE false"
            )
        return con


def seal_options_store(
    source: OptionsStore,
    *,
    research: OptionsStore,
    holdout: OptionsStore,
    fraction: float,
) -> dict[str, HoldoutWindow]:
    """Seal the raw options store: reserve each ``(venue, underlying)`` series' OWN rolled-forward
    holdout tail (by ``trade_date``) — the options analogue of
    :func:`~alpha_core.data.holdout.seal_cold_store`, under the SAME discipline:

    - research quotes -> ``research`` (what discovery reads), the recent tail -> ``holdout``
      (gate-only, a disjoint root); both targets rebuilt from scratch;
    - **the boundary is pinned monotonic (TEST-3)**: each series' window start is floored at the
      previous seal's start (``_windows.json``, read BEFORE the rebuild), so re-sealing over
      backward-extended history can never hand researched quotes to the holdout; a never-sealed
      series inherits its venue's same-interval sibling floor (options manifests key
      ``"venue|underlying|86400"`` — EOD chains are daily-labelled);
    - the manifest lives at the OPTIONS holdout root: a separate seal domain from the bar stores
      (separate roots, separate manifests — no cross-talk with ``data_holdout``).

    The first options seal has no prior manifest -> fresh fraction-of-span boundaries: a genuinely
    NEVER-READ holdout. Returns the per-series windows.
    """
    assert_disjoint_roots(source.root, research.root)
    assert_disjoint_roots(source.root, holdout.root)
    assert_disjoint_roots(research.root, holdout.root)
    prior = prior_window_starts(holdout.root)  # BEFORE the rebuild — the monotonic floor
    for target in (research, holdout):
        for parquet in target.root.glob("*.parquet"):
            parquet.unlink()
    windows: dict[str, HoldoutWindow] = {}
    for underlying, venue in source.series():
        quotes = source.read(underlying=underlying, venue=venue)
        window = compute_holdout_window([q.trade_date for q in quotes], fraction=fraction)
        if window is None:  # pragma: no cover - a listed series always has >=1 quote
            continue
        key = f"{venue.value}|{underlying}|86400"
        window = floored_window(window, key, venue, 86400, prior)
        research.write([q for q in quotes if not window.contains(q.trade_date)])
        holdout.write([q for q in quotes if window.contains(q.trade_date)])
        windows[key] = window
    write_seal_manifest(holdout.root, windows)
    return windows
