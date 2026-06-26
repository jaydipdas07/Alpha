"""CPCV compute budget + carry-forward (R7, B1a.8) — run a night's CPCV queue within a
wall-clock budget across a parallel pool, **carrying** (never truncating) the unfinished rest.

CPCV/PBO is expensive (a backtest per combinatorial split); a night can only afford so many
candidates. This drains a **persistent** queue of pre-screen survivors through an executor
until a wall-clock budget is spent, then **stops submitting** and leaves the rest pending —
the carried queue the next run resumes from. Persistence *is* the carry-forward.

Pure-stdlib (``sqlite3`` + ``concurrent.futures``). ``drain_within_budget`` takes any
``Executor``: pass a :class:`SerialExecutor` for a deterministic serial run, or — in
production — a ``ProcessPoolExecutor`` for true parallelism (CPCV is CPU-bound, so processes,
not threads). The injected ``clock`` keeps the budget testable. Research-plane only.

``run_one(candidate_id, payload)`` must be **idempotent** (carried/failed candidates are
re-run on the next night) and, for a ``ProcessPoolExecutor``, **picklable** (a module-level
function); ``payload`` must be JSON-serializable (it round-trips through the queue).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Executor, Future, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)


class CpcvQueue:
    """A persistent FIFO of candidates awaiting CPCV (SQLite). A candidate not completed this
    run stays ``pending`` for the next — persistence is the carry-forward, never a truncation.
    A candidate whose run raised is moved to a terminal ``failed`` state (quarantined, not
    re-run) so one poison candidate can't stall the whole queue."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path), timeout=30.0)  # 30s busy-wait on contention
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cpcv_queue ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT UNIQUE NOT NULL, "
            "payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending')"
        )
        self._conn.commit()

    def enqueue(self, candidate_id: str, payload: dict[str, Any]) -> None:
        """Add a candidate (idempotent on ``candidate_id``; re-enqueue is a no-op).
        ``payload`` must be JSON-serializable."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO cpcv_queue (candidate_id, payload) VALUES (?, ?) "
                "ON CONFLICT(candidate_id) DO NOTHING",
                (candidate_id, json.dumps(payload)),
            )

    def pending(self) -> list[str]:
        """Candidate ids still awaiting CPCV, in enqueue order (FIFO) — excludes done/failed."""
        rows = self._conn.execute(
            "SELECT candidate_id FROM cpcv_queue WHERE status = 'pending' ORDER BY seq"
        ).fetchall()
        return [str(r[0]) for r in rows]

    def payload(self, candidate_id: str) -> dict[str, Any]:
        """The enqueued payload for a candidate (``KeyError`` if unknown)."""
        row = self._conn.execute(
            "SELECT payload FROM cpcv_queue WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        result: dict[str, Any] = json.loads(row[0])
        return result

    def _set_status(self, candidate_id: str, status: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE cpcv_queue SET status = ? WHERE candidate_id = ?", (status, candidate_id)
            )

    def mark_done(self, candidate_id: str) -> None:
        """Mark a candidate completed (removes it from the carried queue)."""
        self._set_status(candidate_id, "done")

    def mark_failed(self, candidate_id: str) -> None:
        """Quarantine a candidate whose run raised — terminal, never re-run (and excluded
        from the carried queue)."""
        self._set_status(candidate_id, "failed")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> CpcvQueue:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class SerialExecutor(Executor):
    """A synchronous ``concurrent.futures.Executor`` — runs each task inline and returns an
    already-completed future. Deterministic (for serial runs + tests); production passes a
    ``ProcessPoolExecutor`` instead for real parallelism."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        future: Future[Any] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # pragma: no cover - mirrors Executor semantics
            future.set_exception(exc)
        return future


@dataclass(frozen=True, slots=True)
class BudgetResult:
    """The outcome of one budgeted drain."""

    completed: tuple[str, ...]  # candidates run + marked done this session
    failed: tuple[str, ...]  # candidates whose run raised — quarantined, not re-run
    carried: tuple[str, ...]  # candidates left pending — carried to the next run
    elapsed: float


def drain_within_budget(
    queue: CpcvQueue,
    run_one: Callable[[str, dict[str, Any]], Any],
    *,
    budget_seconds: float,
    executor: Executor,
    max_in_flight: int,
    clock: Callable[[], float] = time.monotonic,
) -> BudgetResult:
    """Run pending candidates' CPCV (``run_one(candidate_id, payload)``) through ``executor``,
    keeping up to ``max_in_flight`` in flight, until ``budget_seconds`` of wall-clock elapses.
    Once the budget is spent, **no new candidate is submitted** — already-running ones finish,
    and everything not yet submitted stays pending (the carried queue, never truncated). A
    candidate whose run raises is **quarantined** (logged + marked failed) and the drain
    continues, so one poison candidate can't stall the night. ``clock`` is injected so the
    budget is deterministic in tests."""
    if budget_seconds <= 0:
        raise ValueError(f"budget_seconds must be > 0; got {budget_seconds}")
    if max_in_flight < 1:
        raise ValueError(f"max_in_flight must be >= 1; got {max_in_flight}")
    start = clock()
    completed: list[str] = []
    failed: list[str] = []
    queued = iter(queue.pending())
    in_flight: dict[Future[Any], str] = {}

    def _submit_next() -> None:
        if clock() - start >= budget_seconds:
            return  # budget spent -> submit nothing more (the rest stay pending = carried)
        cid = next(queued, None)
        if cid is not None:
            in_flight[executor.submit(run_one, cid, queue.payload(cid))] = cid

    for _ in range(max_in_flight):
        _submit_next()
    while in_flight:
        done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
        for future in done:
            cid = in_flight.pop(future)
            try:
                future.result()
            except Exception as exc:
                queue.mark_failed(cid)
                failed.append(cid)
                _log.warning("cpcv candidate failed", candidate_id=cid, error=str(exc))
            else:
                queue.mark_done(cid)
                completed.append(cid)
            _submit_next()

    return BudgetResult(tuple(completed), tuple(failed), tuple(queue.pending()), clock() - start)
