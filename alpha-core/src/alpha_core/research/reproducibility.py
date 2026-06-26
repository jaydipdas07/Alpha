"""Reproducibility ledger (B1a.7) — the provenance that makes a backtest bit-for-bit
reproducible from its record.

A backtest is reproducible iff all its inputs can be reconstructed. This records the
identity of every input — engine version, dataset content hash, cost-model version,
strategy id + params, RNG seed — into one :class:`ReproRecord` whose ``fingerprint`` keys
the run. The alpha_core engine is already deterministic (injected clock, ``Decimal`` math)
and the cold store byte-stable (B1a.1), so the same record yields the same result.

:class:`ReproLedger` (SQLite, stdlib) persists each run's record + the result hash it
produced, keyed by fingerprint, and **enforces** reproducibility: recording the same
inputs with a *different* result raises (the engine went non-deterministic). Mirrors the
pod ``backtests`` provenance columns. Research-plane only — ``hash_result`` reads a
``BacktestResult`` but the worker never imports this module.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from alpha_core.backtest.runner import BacktestResult
from alpha_core.core.models import Bar


def engine_version() -> str:
    """The installed alpha-core version — the engine identity in the record."""
    try:
        return version("alpha-core")
    except PackageNotFoundError:  # pragma: no cover - source checkout without an install
        return "0+unknown"


def _canonical(obj: Any) -> Any:
    """Canonicalize for a stable hash: ``Decimal`` -> exact string, mappings key-sorted."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, Mapping):
        return {str(k): _canonical(obj[k]) for k in sorted(obj, key=str)}
    if isinstance(obj, list | tuple):
        return [_canonical(x) for x in obj]
    return obj


def _digest(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(_canonical(obj), sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:16]


def hash_mapping(mapping: Mapping[str, Any]) -> str:
    """A stable content hash of a config mapping (Decimal-exact, key-order-independent)."""
    return _digest(mapping)


def hash_bars(bars: Sequence[Bar]) -> str:
    """A stable content hash of an input bar series — the dataset version/identity."""
    return _digest(
        [
            (
                b.symbol,
                b.venue.value,
                b.asset_class.value,
                b.start.isoformat(),
                int(b.interval.total_seconds()),
                str(b.open),
                str(b.high),
                str(b.low),
                str(b.close),
                str(b.volume),
            )
            for b in bars
        ]
    )


def hash_result(result: BacktestResult) -> str:
    """A stable hash of a backtest's output — equal iff two runs are bit-for-bit identical."""
    s = result.stats
    return _digest(
        {
            "final_pnl": str(s.final_pnl),
            "total_fees": str(s.total_fees),
            "num_fills": s.num_fills,
            "funding_paid": str(s.funding_paid),
            "traded_notional": str(s.traded_notional),
            "halted": result.halted,
            "equity_curve": [(ts.isoformat(), str(pnl)) for ts, pnl in result.equity_curve],
        }
    )


@dataclass(frozen=True, slots=True)
class ReproRecord:
    """The provenance that reproduces a backtest bit-for-bit (B1a.7)."""

    engine_version: str
    dataset_hash: str
    cost_model_version: str
    strategy_id: str
    strategy_params_hash: str
    rng_seed: int

    @property
    def fingerprint(self) -> str:
        """A single deterministic key over the whole record — equal iff every input is."""
        return _digest(asdict(self))


def record_backtest(
    *,
    bars: Sequence[Bar],
    cost_config: Mapping[str, Any],
    strategy_id: str,
    strategy_params: Mapping[str, Any],
    rng_seed: int,
) -> ReproRecord:
    """Assemble the reproducibility record for a backtest from its inputs."""
    return ReproRecord(
        engine_version=engine_version(),
        dataset_hash=hash_bars(bars),
        cost_model_version=hash_mapping(cost_config),
        strategy_id=strategy_id,
        strategy_params_hash=hash_mapping(strategy_params),
        rng_seed=rng_seed,
    )


class ReproLedger:
    """SQLite-backed ledger of each backtest's provenance + the result hash it produced,
    keyed by fingerprint. It enforces reproducibility: re-recording the same inputs with a
    different result raises; :meth:`verify` re-checks a fresh result against the original."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path), timeout=30.0)  # 30s busy-wait on contention
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS repro_ledger ("
            "fingerprint TEXT PRIMARY KEY, engine_version TEXT NOT NULL, "
            "dataset_hash TEXT NOT NULL, cost_model_version TEXT NOT NULL, "
            "strategy_id TEXT NOT NULL, strategy_params_hash TEXT NOT NULL, "
            "rng_seed INTEGER NOT NULL, result_hash TEXT NOT NULL)"
        )
        self._conn.commit()

    def record(self, rec: ReproRecord, result_hash: str) -> None:
        """Persist a run's provenance + result hash. Idempotent on identical inputs+result;
        raises on the same fingerprint with a *different* result (a reproducibility break —
        the engine went non-deterministic)."""
        existing = self.get(rec.fingerprint)
        if existing is not None:
            if existing[1] != result_hash:
                raise ValueError(
                    f"reproducibility violation: fingerprint {rec.fingerprint} previously "
                    f"produced result {existing[1]} but now {result_hash} — the same inputs gave "
                    "a different result (the engine is non-deterministic)"
                )
            return  # already recorded, same result -> idempotent no-op
        with self._conn:
            self._conn.execute(
                "INSERT INTO repro_ledger (fingerprint, engine_version, dataset_hash, "
                "cost_model_version, strategy_id, strategy_params_hash, rng_seed, result_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rec.fingerprint,
                    rec.engine_version,
                    rec.dataset_hash,
                    rec.cost_model_version,
                    rec.strategy_id,
                    rec.strategy_params_hash,
                    rec.rng_seed,
                    result_hash,
                ),
            )

    def get(self, fingerprint: str) -> tuple[ReproRecord, str] | None:
        """The recorded ``(ReproRecord, result_hash)`` for a fingerprint, or ``None``."""
        row = self._conn.execute(
            "SELECT engine_version, dataset_hash, cost_model_version, strategy_id, "
            "strategy_params_hash, rng_seed, result_hash FROM repro_ledger WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if row is None:
            return None
        rec = ReproRecord(
            engine_version=str(row[0]),
            dataset_hash=str(row[1]),
            cost_model_version=str(row[2]),
            strategy_id=str(row[3]),
            strategy_params_hash=str(row[4]),
            rng_seed=int(row[5]),
        )
        return rec, str(row[6])

    def verify(self, rec: ReproRecord, result_hash: str) -> bool:
        """True iff this record was recorded and re-ran to the SAME result hash (reproduced);
        False on an unseen record or a result-hash mismatch (a reproducibility break)."""
        stored = self.get(rec.fingerprint)
        return stored is not None and stored[1] == result_hash

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ReproLedger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
