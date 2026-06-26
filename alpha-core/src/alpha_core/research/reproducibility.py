"""Reproducibility ledger (B1a.7) — the provenance that makes a backtest bit-for-bit
reproducible from its record.

A backtest is reproducible iff **all** its inputs can be reconstructed. :class:`ReproRecord`
records the identity of every input ``run_backtest`` takes — engine version, dataset hash,
instruments, risk config, cost-model version, strategy id + params, RNG seed, venue,
starting cash, the 2x-stress flag, and perp funding — into one record whose ``fingerprint``
keys the run (equal iff every input is). The alpha_core engine is already deterministic
(injected clock, ``Decimal`` math) and the cold store byte-stable (B1a.1), so the same
record yields the same result.

:class:`ReproLedger` (SQLite, stdlib) persists each run's record + the result hash it
produced, keyed by fingerprint, and **enforces** reproducibility: recording the same inputs
with a *different* result raises (the engine went non-deterministic). ``run_and_record``
assembles the record from the **same** arguments it passes to ``run_backtest``, so the record
can never drift from the run. Mirrors the pod ``backtests`` provenance columns; research-plane
only — it reads ``BacktestResult`` but the worker never imports this module.
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

from pydantic import BaseModel

from alpha_core.backtest.runner import BacktestResult, run_backtest
from alpha_core.core.enums import Venue
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.execution.funding import FundingConfig
from alpha_core.risk.limits import RiskConfig


def engine_version() -> str:
    """The installed alpha-core version — the engine identity in the record."""
    try:
        return version("alpha-core")
    except PackageNotFoundError:  # pragma: no cover - source checkout without an install
        return "0+unknown"


def _canonical(obj: Any) -> Any:
    """Canonicalize for a stable, cross-process hash: ``Decimal`` -> exact string, mappings
    key-sorted, sets order-normalized (never PYTHONHASHSEED-dependent)."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, Mapping):
        return {str(k): _canonical(obj[k]) for k in sorted(obj, key=str)}
    if isinstance(obj, set | frozenset):
        return sorted((_canonical(x) for x in obj), key=str)
    if isinstance(obj, list | tuple):
        return [_canonical(x) for x in obj]
    return obj


def _digest(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(_canonical(obj), sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:32]  # 128-bit — collision-free for any realistic ledger size


def hash_mapping(mapping: Mapping[str, Any]) -> str:
    """A stable content hash of a config mapping (Decimal-exact, key-order-independent)."""
    return _digest(mapping)


def _hash_model(model: BaseModel) -> str:
    """A stable content hash of a pydantic model (Decimal-exact via ``model_dump``)."""
    return _digest(model.model_dump())


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
    """A stable hash of a backtest's full output — equal iff two runs produced the identical
    report. Decimal fields are str-exact; the derived float stats (return %, drawdown, Sharpe,
    win rate, turnover) are included via their round-tripping ``str`` so nothing is dropped."""
    s = result.stats
    return _digest(
        {
            "final_pnl": str(s.final_pnl),
            "total_fees": str(s.total_fees),
            "num_fills": s.num_fills,
            "funding_paid": str(s.funding_paid),
            "traded_notional": str(s.traded_notional),
            "total_return_pct": str(s.total_return_pct),
            "max_drawdown_pct": str(s.max_drawdown_pct),
            "sharpe": str(s.sharpe),
            "win_rate": str(s.win_rate),
            "turnover_ratio": str(s.turnover_ratio),
            "halted": result.halted,
            "equity_curve": [(ts.isoformat(), str(pnl)) for ts, pnl in result.equity_curve],
        }
    )


@dataclass(frozen=True, slots=True)
class ReproRecord:
    """The provenance that reproduces a backtest bit-for-bit (B1a.7) — every result-affecting
    input ``run_backtest`` takes.

    Strategies are pinned by ``strategy_id`` + ``strategy_params`` (the config-driven
    ``strategies/<name>.yaml`` convention) + the seed, not by their source code — so editing a
    custom ``Strategy``'s logic *without* bumping its id/params is recorded as the same run
    (and would surface later as a :class:`ReproLedger` reproducibility violation)."""

    engine_version: str
    dataset_hash: str
    instruments_hash: str
    risk_config_hash: str
    cost_model_version: str
    strategy_id: str
    strategy_params_hash: str
    rng_seed: int
    venue: str
    starting_cash: str
    stress: bool
    funding_hash: str

    @property
    def fingerprint(self) -> str:
        """A single deterministic key over the whole record — equal iff every input is."""
        return _digest(asdict(self))


def record_backtest(
    *,
    bars: Sequence[Bar],
    instruments: Mapping[str, InstrumentMeta],
    risk_config: RiskConfig,
    cost_config: Mapping[str, Any],
    strategy_id: str,
    strategy_params: Mapping[str, Any],
    rng_seed: int,
    venue: Venue,
    starting_cash: Decimal,
    stress: bool = False,
    funding: FundingConfig | None = None,
) -> ReproRecord:
    """Assemble the reproducibility record from a backtest's full set of inputs."""
    return ReproRecord(
        engine_version=engine_version(),
        dataset_hash=hash_bars(bars),
        instruments_hash=hash_mapping({k: v.model_dump() for k, v in instruments.items()}),
        risk_config_hash=_hash_model(risk_config),
        cost_model_version=hash_mapping(cost_config),
        strategy_id=strategy_id,
        strategy_params_hash=hash_mapping(strategy_params),
        rng_seed=rng_seed,
        venue=venue.value,
        starting_cash=str(starting_cash),
        stress=stress,
        funding_hash="none" if funding is None else _hash_model(funding),
    )


async def run_and_record(
    *,
    bars: list[Bar],
    strategy: Strategy,
    instruments: Mapping[str, InstrumentMeta],
    risk_config: RiskConfig,
    cost_config: Mapping[str, Any],
    strategy_id: str,
    strategy_params: Mapping[str, Any],
    rng_seed: int,
    venue: Venue = Venue.NSE,
    starting_cash: Decimal = Decimal("1000000"),
    stress: bool = False,
    funding: FundingConfig | None = None,
    ledger: ReproLedger | None = None,
) -> tuple[BacktestResult, ReproRecord]:
    """Run a backtest and assemble its reproducibility record from the **same** inputs, so the
    record can never drift from the run. Optionally records ``(record, hash_result)`` to
    ``ledger`` (which then enforces reproducibility on any re-run)."""
    result = await run_backtest(
        bars=bars,
        strategy=strategy,
        instruments=dict(instruments),
        risk_config=risk_config,
        cost_config=dict(cost_config),
        venue=venue,
        starting_cash=starting_cash,
        stress=stress,
        funding=funding,
    )
    record = record_backtest(
        bars=bars,
        instruments=instruments,
        risk_config=risk_config,
        cost_config=cost_config,
        strategy_id=strategy_id,
        strategy_params=strategy_params,
        rng_seed=rng_seed,
        venue=venue,
        starting_cash=starting_cash,
        stress=stress,
        funding=funding,
    )
    if ledger is not None:
        ledger.record(record, hash_result(result))
    return result, record


class ReproLedger:
    """SQLite-backed ledger of each backtest's provenance (as JSON) + the result hash it
    produced, keyed by fingerprint. It enforces reproducibility: re-recording the same inputs
    with a different result raises; :meth:`verify` re-checks a fresh result against the original."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path), timeout=30.0)  # 30s busy-wait on contention
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS repro_ledger ("
            "fingerprint TEXT PRIMARY KEY, record_json TEXT NOT NULL, result_hash TEXT NOT NULL)"
        )
        self._conn.commit()

    def record(self, rec: ReproRecord, result_hash: str) -> None:
        """Persist a run's provenance + result hash. Idempotent on identical inputs+result;
        raises on the same fingerprint with a *different* result (a reproducibility break —
        the same inputs gave a different result, so the engine went non-deterministic)."""
        existing = self.get(rec.fingerprint)
        if existing is not None:
            self._reject_if_drifted(rec.fingerprint, existing[1], result_hash)
            return  # already recorded, same result -> idempotent no-op
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO repro_ledger (fingerprint, record_json, result_hash) "
                    "VALUES (?, ?, ?)",
                    (rec.fingerprint, json.dumps(asdict(rec)), result_hash),
                )
        except sqlite3.IntegrityError:  # pragma: no cover - a concurrent writer won the PK race
            stored = self.get(rec.fingerprint)
            if stored is not None:
                self._reject_if_drifted(rec.fingerprint, stored[1], result_hash)

    @staticmethod
    def _reject_if_drifted(fingerprint: str, stored_hash: str, fresh_hash: str) -> None:
        if stored_hash != fresh_hash:
            raise ValueError(
                f"reproducibility violation: fingerprint {fingerprint} previously produced "
                f"result {stored_hash} but now {fresh_hash} — the same inputs gave a different "
                "result (the engine is non-deterministic)"
            )

    def get(self, fingerprint: str) -> tuple[ReproRecord, str] | None:
        """The recorded ``(ReproRecord, result_hash)`` for a fingerprint, or ``None``."""
        row = self._conn.execute(
            "SELECT record_json, result_hash FROM repro_ledger WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if row is None:
            return None
        d = json.loads(row[0])
        rec = ReproRecord(
            engine_version=str(d["engine_version"]),
            dataset_hash=str(d["dataset_hash"]),
            instruments_hash=str(d["instruments_hash"]),
            risk_config_hash=str(d["risk_config_hash"]),
            cost_model_version=str(d["cost_model_version"]),
            strategy_id=str(d["strategy_id"]),
            strategy_params_hash=str(d["strategy_params_hash"]),
            rng_seed=int(d["rng_seed"]),
            venue=str(d["venue"]),
            starting_cash=str(d["starting_cash"]),
            stress=bool(d["stress"]),
            funding_hash=str(d["funding_hash"]),
        )
        return rec, str(row[1])

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
