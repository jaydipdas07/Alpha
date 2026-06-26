"""Keyed persistent trial ledger (R4, B1a.5) — the cumulative trial count the Deflated
Sharpe Ratio consumes, keyed by ``(market, family, window)`` so the multiple-testing
penalty for one search cell is invariant to trials run in any *other* cell.

Persisted in SQLite (stdlib, atomic upsert-increments — no lost updates across
processes), mirroring the pod ``research_ledger`` table: ``cell_key`` =
``"market|family|window"`` (lowercase market, the pod ENUM crypto/equity/index_option)
+ ``cumulative_trials``. The pod table is the display mirror; this is the research-box
source of truth the rigor gate reads/increments. No money, no clock — just counts.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from alpha_core.core.enums import AssetClass


def cell_key(market: AssetClass, family: str, window: str) -> str:
    """The composite ledger key ``"market|family|window"`` (lowercase market, matching
    the pod ``research_ledger`` ENUM). ``family`` / ``window`` may not contain ``|`` and
    must fit the pod's 64-char column limit (so the key round-trips on pod sync)."""
    if "|" in family or "|" in window:
        raise ValueError("family/window must not contain the '|' key separator")
    if len(family) > 64 or len(window) > 64:
        raise ValueError("family and window must each be <= 64 chars (the pod column limit)")
    return f"{market.value.lower()}|{family}|{window}"


@dataclass(frozen=True, slots=True)
class CellCount:
    """A ledger cell and its cumulative trial count."""

    market: AssetClass
    family: str
    window: str
    cumulative_trials: int


class TrialLedger:
    """SQLite-backed cumulative trial counter keyed by ``(market, family, window)``."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path), timeout=30.0)  # 30s busy-wait on contention
        self._conn.execute("PRAGMA journal_mode=WAL")  # concurrent writers serialize cleanly
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS research_ledger ("
            "cell_key TEXT PRIMARY KEY, market TEXT NOT NULL, family TEXT NOT NULL, "
            "window TEXT NOT NULL, cumulative_trials INTEGER NOT NULL DEFAULT 0)"
        )
        self._conn.commit()

    def increment(self, market: AssetClass, family: str, window: str, *, by: int = 1) -> int:
        """Atomically add ``by`` to the cell's cumulative trial count; return the new total
        (read back via ``RETURNING``, tied to this transaction). The upsert is one
        read-modify-write under SQLite's write lock, so concurrent increments never lose
        updates."""
        if by < 1:
            raise ValueError(f"increment `by` must be >= 1; got {by}")
        key = cell_key(market, family, window)
        with self._conn:  # transaction = atomic upsert
            cursor = self._conn.execute(
                "INSERT INTO research_ledger (cell_key, market, family, window, cumulative_trials) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(cell_key) "
                "DO UPDATE SET cumulative_trials = cumulative_trials + excluded.cumulative_trials "
                "RETURNING cumulative_trials",
                (key, market.value.lower(), family, window, by),
            )
            total = cursor.fetchone()[0]
        return int(total)

    def count(self, market: AssetClass, family: str, window: str) -> int:
        """The cumulative trial count for this cell (0 if never seen)."""
        row = self._conn.execute(
            "SELECT cumulative_trials FROM research_ledger WHERE cell_key = ?",
            (cell_key(market, family, window),),
        ).fetchone()
        return int(row[0]) if row else 0

    def cells(self) -> list[CellCount]:
        """Every recorded cell (for sync to the pod / inspection), ordered by key."""
        rows = self._conn.execute(
            "SELECT market, family, window, cumulative_trials "
            "FROM research_ledger ORDER BY cell_key"
        ).fetchall()
        return [CellCount(AssetClass(m.upper()), f, w, int(c)) for m, f, w, c in rows]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> TrialLedger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
