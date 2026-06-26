"""CPCV compute budget + carry-forward (R7, B1a.8) — budget respected, queue carried."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from alpha_core.helpers.config import CpcvBudgetConfig, load_rigor_config
from alpha_core.research.cpcv_budget import (
    CpcvQueue,
    SerialExecutor,
    drain_within_budget,
)


class _Clock:
    """A controllable monotonic clock; ``run_one`` advances it to simulate compute cost."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# --- the persistent queue ------------------------------------------------------


def test_queue_enqueue_pending_payload_mark_done() -> None:
    with CpcvQueue() as q:
        q.enqueue("a", {"k": 1})
        q.enqueue("b", {"k": 2})
        q.enqueue("a", {"k": 99})  # idempotent on candidate_id -> no-op, payload unchanged
        assert q.pending() == ["a", "b"]  # FIFO (enqueue order)
        assert q.payload("a") == {"k": 1}
        q.mark_done("a")
        assert q.pending() == ["b"]
        with pytest.raises(KeyError):
            q.payload("missing")


def test_queue_persists_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    with CpcvQueue(db) as q:
        q.enqueue("a", {})
        q.enqueue("b", {})
        q.mark_done("a")
    with CpcvQueue(db) as q:
        assert q.pending() == ["b"]  # the unfinished one survived


# --- the budget drain ----------------------------------------------------------


def test_drain_respects_the_budget_and_carries_the_rest() -> None:
    clock = _Clock()
    ran: list[str] = []

    def run_one(cid: str, _payload: dict[str, Any]) -> None:
        clock.t += 2.0  # each candidate costs 2s of the budget
        ran.append(cid)

    with CpcvQueue() as q:
        for c in "abcd":
            q.enqueue(c, {"x": c})
        result = drain_within_budget(
            q, run_one, budget_seconds=5.0, executor=SerialExecutor(), max_in_flight=1, clock=clock
        )
        # a(2s), b(4s), c(6s) submit while elapsed < 5; at 6s the budget is spent -> stop.
        assert result.completed == ("a", "b", "c")
        assert result.carried == ("d",)  # the rest carried, NOT truncated
        assert ran == ["a", "b", "c"]
        assert q.pending() == ["d"]


def test_carry_forward_resumes_on_the_next_run(tmp_path: Path) -> None:
    db = tmp_path / "q.db"

    def costly(cid: str, _p: dict[str, Any], clock: _Clock) -> None:
        clock.t += 2.0

    clock1 = _Clock()
    with CpcvQueue(db) as q:
        for c in "abcd":
            q.enqueue(c, {})
        r1 = drain_within_budget(
            q,
            lambda c, p: costly(c, p, clock1),
            budget_seconds=5.0,
            executor=SerialExecutor(),
            max_in_flight=1,
            clock=clock1,
        )
        assert r1.carried == ("d",)

    clock2 = _Clock()
    with CpcvQueue(db) as q:  # a fresh night, fresh budget
        assert q.pending() == ["d"]  # carried, persisted
        r2 = drain_within_budget(
            q,
            lambda c, p: costly(c, p, clock2),
            budget_seconds=5.0,
            executor=SerialExecutor(),
            max_in_flight=1,
            clock=clock2,
        )
        assert r2.completed == ("d",) and r2.carried == ()
        assert q.pending() == []


def test_drain_with_a_real_parallel_pool_completes_within_budget() -> None:
    ran: list[str] = []
    lock = threading.Lock()

    def run_one(cid: str, _payload: dict[str, Any]) -> None:
        with lock:
            ran.append(cid)

    with CpcvQueue() as q:
        for i in range(8):
            q.enqueue(f"c{i}", {})
        with ThreadPoolExecutor(max_workers=3) as pool:
            result = drain_within_budget(
                q,
                run_one,
                budget_seconds=1e9,  # generous -> everything runs
                executor=pool,
                max_in_flight=3,
                clock=time.monotonic,
            )
    assert len(result.completed) == 8 and result.carried == ()
    assert sorted(ran) == sorted(f"c{i}" for i in range(8))


def test_a_failing_candidate_is_quarantined_not_a_stall(tmp_path: Path) -> None:
    db = tmp_path / "q.db"

    def run_one(cid: str, _payload: dict[str, Any]) -> None:
        if cid == "b":
            raise RuntimeError("boom")  # a poison candidate

    with CpcvQueue(db) as q:
        for c in "abc":
            q.enqueue(c, {})
        result = drain_within_budget(
            q,
            run_one,
            budget_seconds=1e9,
            executor=SerialExecutor(),
            max_in_flight=1,
            clock=time.monotonic,
        )
        assert result.completed == ("a", "c")  # a and c ran despite b failing (no abort/stall)
        assert result.failed == ("b",)  # b is quarantined...
        assert result.carried == ()
        assert q.pending() == []
    with CpcvQueue(db) as q:  # ... and the next night does not resurrect the poison candidate
        assert q.pending() == []


def test_drain_rejects_bad_budget_and_concurrency() -> None:
    with CpcvQueue() as q:
        with pytest.raises(ValueError, match="max_in_flight"):
            drain_within_budget(
                q, lambda c, p: None, budget_seconds=1.0, executor=SerialExecutor(), max_in_flight=0
            )
        with pytest.raises(ValueError, match="budget_seconds"):
            drain_within_budget(
                q, lambda c, p: None, budget_seconds=0.0, executor=SerialExecutor(), max_in_flight=1
            )


# --- config --------------------------------------------------------------------


def test_cpcv_budget_config() -> None:
    cfg = load_rigor_config()
    assert cfg.cpcv_budget.budget_seconds > 0
    assert cfg.cpcv_budget.pool_size >= 1
    with pytest.raises(ValidationError):
        CpcvBudgetConfig(budget_seconds=0.0, pool_size=4)
