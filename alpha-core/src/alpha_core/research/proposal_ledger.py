"""Persistent proposal-fingerprint ledger (B1b.3 precondition) — the originality store the
strategist hydrates from, and the *idempotent* trial count the Deflated Sharpe Ratio deflates by.

The B1b.1b review flagged a desync hazard: the strategist's ``seen`` set was in-memory while the
trial count was durable, so a discovery loop that forgot to hydrate ``seen`` would re-propose a
config **and re-increment the trial count**, corrupting the per-cell multiple-testing penalty (R4).
This store closes that by construction: a cell's trial count *is* the number of **distinct
fingerprints** recorded for it, so recording the same proposal twice is a no-op. There is no
counter to drift from the fingerprint set — they are the same thing.

Keyed by ``(market, family, window)`` (the same cell as the trial ledger, via ``cell_key``), so a
cell's count is invariant to proposals in any *other* cell. SQLite (stdlib), WAL, one atomic
upsert per record — safe across the discovery pool's processes. No money, no clock — just the
durable set of what's been tried.

Wiring (B1b.3b): the ``strategist`` will ``record`` each candidate here (originality via
``is_new``, the trial index via ``count``) instead of an in-memory ``seen`` + a bare counter, and
the discovery loop reads ``count`` for the DSR deflation. This module is that store, landed first.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from alpha_core.core.enums import AssetClass
from alpha_core.research.trial_ledger import CellCount, cell_key


@dataclass(frozen=True, slots=True)
class RecordResult:
    """The outcome of recording a proposal: whether it was new to the cell, and the cell's
    resulting trial count (the number of distinct fingerprints)."""

    is_new: bool
    count: int


class ProposalLedger:
    """SQLite-backed set of proposal fingerprints per ``(market, family, window)`` cell. The cell's
    trial count is the size of that set, so recording is idempotent (no double-counting)."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path), timeout=30.0)  # 30s busy-wait on contention
        self._conn.execute("PRAGMA journal_mode=WAL")  # concurrent writers serialize cleanly
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS proposals ("
            "cell_key TEXT NOT NULL, market TEXT NOT NULL, family TEXT NOT NULL, "
            "window TEXT NOT NULL, fingerprint TEXT NOT NULL, "
            "PRIMARY KEY (cell_key, fingerprint))"
        )
        self._conn.commit()

    def record(
        self, market: AssetClass, family: str, window: str, fingerprint: str
    ) -> RecordResult:
        """Record ``fingerprint`` as tried in the cell and return ``(is_new, count)``. Idempotent:
        a fingerprint already present is a no-op (``is_new=False``) and the count is unchanged, so a
        re-proposed config never inflates the trial count. Insert + count are one transaction."""
        key = cell_key(market, family, window)
        with self._conn:  # one transaction: idempotent insert, then the authoritative count
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO proposals (cell_key, market, family, window, fingerprint) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, market.value.lower(), family, window, fingerprint),
            )
            is_new = cursor.rowcount == 1
            count = self._conn.execute(
                "SELECT count(*) FROM proposals WHERE cell_key = ?", (key,)
            ).fetchone()[0]
        return RecordResult(is_new=is_new, count=int(count))

    def seen(self, market: AssetClass, family: str, window: str) -> set[str]:
        """The set of fingerprints already tried in the cell (hydrates the strategist's originality
        check across runs)."""
        rows = self._conn.execute(
            "SELECT fingerprint FROM proposals WHERE cell_key = ?",
            (cell_key(market, family, window),),
        ).fetchall()
        return {r[0] for r in rows}

    def count(self, market: AssetClass, family: str, window: str) -> int:
        """The cell's trial count — the number of distinct fingerprints (0 if never seen). This is
        the count the DSR deflates by (R4)."""
        row = self._conn.execute(
            "SELECT count(*) FROM proposals WHERE cell_key = ?",
            (cell_key(market, family, window),),
        ).fetchone()
        return int(row[0])

    def cells(self) -> list[CellCount]:
        """Every recorded cell with its trial count (for sync to the pod / inspection)."""
        rows = self._conn.execute(
            "SELECT market, family, window, count(*) FROM proposals "
            "GROUP BY cell_key, market, family, window ORDER BY cell_key"
        ).fetchall()
        return [CellCount(AssetClass(m.upper()), f, w, int(c)) for m, f, w, c in rows]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ProposalLedger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
