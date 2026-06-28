"""Pod-sync tests — the best-effort ``worker_status`` writer + the pod-client gate.

The writer must NEVER raise on a pod error (TEST-8: Lemma off the money path) and must
upsert by the unique ``worker_id`` (find-or-create, then update the cached row).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from lemma_sdk import Pod

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Position
from alpha_core.execution.commands import CommandKind, CommandStatus
from worker.config import EnvConfig
from worker.pod_sync import (
    PodCommandSource,
    PodStatusWriter,
    build_pod_client,
    positions_hash,
    read_envfile_token,
)

NOW = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)


class _FakeListResp:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items

    def to_dict(self) -> dict[str, Any]:
        return {"items": self._items}


class _FakeRecords:
    def __init__(self, existing: list[dict[str, Any]], *, fail: bool) -> None:
        self._existing = existing
        self._fail = fail
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.updated: list[tuple[str, str, dict[str, Any]]] = []

    def list(self, table: str, *, limit: int = 20, **_kw: Any) -> _FakeListResp:
        return _FakeListResp(self._existing)

    def create(self, table: str, data: dict[str, Any]) -> dict[str, Any]:
        if self._fail:
            raise RuntimeError("pod down")
        self.created.append((table, data))
        return {**data, "id": "row-1"}

    def update(self, table: str, record_id: str, data: dict[str, Any]) -> dict[str, Any]:
        if self._fail:
            raise RuntimeError("pod down")
        self.updated.append((table, record_id, data))
        return {**data, "id": record_id}


class _FakePod:
    def __init__(
        self,
        existing: list[dict[str, Any]] | None = None,
        *,
        fail: bool = False,
        pending: list[dict[str, Any]] | None = None,
        query_fail: bool = False,
    ) -> None:
        self.records = _FakeRecords(existing or [], fail=fail)
        self._pending = pending or []
        self._query_fail = query_fail

    def query(self, sql: str) -> _FakeListResp:
        if self._query_fail:
            raise RuntimeError("pod query down")
        return _FakeListResp(self._pending)


def _pos(symbol: str, qty: str) -> Position:
    held = Decimal(qty) != 0
    return Position(
        venue=Venue.DELTA,
        symbol=symbol,
        asset_class=AssetClass.CRYPTO,
        quantity=Decimal(qty),
        average_price=Decimal("100") if held else None,
        last_price=Decimal("100") if held else None,
        updated_at=NOW,
    )


def _writer(pod: _FakePod) -> PodStatusWriter:
    return PodStatusWriter(cast(Pod, pod), worker_id="w-1", mode="paper")


async def test_first_beat_creates_the_row() -> None:
    pod = _FakePod()  # no existing rows
    await _writer(pod).beat(
        now=NOW, armed=True, positions=[_pos("BTC/USD:USD", "1")], detail={"run_state": "running"}
    )
    assert len(pod.records.created) == 1
    table, data = pod.records.created[0]
    assert table == "worker_status"
    assert data["worker_id"] == "w-1"
    assert data["armed"] is True
    assert data["mode"] == "paper"
    assert data["last_seen"] == NOW.isoformat()
    assert data["detail"] == {"run_state": "running"}
    assert data["positions_hash"]  # a non-empty hash for an open book


async def test_second_beat_updates_the_cached_row() -> None:
    pod = _FakePod()
    writer = _writer(pod)
    await writer.beat(now=NOW, armed=True, positions=[], detail={})
    await writer.beat(now=NOW, armed=False, positions=[], detail={})
    assert len(pod.records.created) == 1  # created once...
    assert len(pod.records.updated) == 1  # ...then reused the cached id
    assert pod.records.updated[0][1] == "row-1"
    assert pod.records.updated[0][2]["armed"] is False


async def test_beat_finds_an_existing_row_and_updates() -> None:
    pod = _FakePod(existing=[{"id": "existing-9", "worker_id": "w-1"}])
    await _writer(pod).beat(now=NOW, armed=True, positions=[], detail={})
    assert pod.records.created == []  # did NOT create a duplicate
    assert pod.records.updated[0][1] == "existing-9"


async def test_beat_swallows_pod_errors() -> None:
    # TEST-8: a pod outage must never raise into the worker loop.
    pod = _FakePod(fail=True)
    await _writer(pod).beat(now=NOW, armed=True, positions=[], detail={})  # must not raise


def test_positions_hash_stable_and_ignores_flat() -> None:
    a = positions_hash([_pos("BTC/USD:USD", "1"), _pos("ETH/USD:USD", "2")])
    b = positions_hash([_pos("ETH/USD:USD", "2"), _pos("BTC/USD:USD", "1")])  # order-independent
    assert a == b
    assert positions_hash([_pos("BTC/USD:USD", "0")]) == positions_hash([])  # zero-qty ignored


def _base_env() -> dict[str, Any]:
    return {
        "env": "paper",
        "mode": "paper",
        "allow_live": False,
        "worker_id": "w",
        "venue": "delta-testnet",
        "strategy": "idle",
        "symbols": ["BTC/USD:USD"],
        "bar_interval_seconds": 60,
        "state_db": "sqlite:///:memory:",
        "heartbeat_path": "/tmp/hb",
        "command_poll_seconds": 1.0,
        "reconcile_interval_seconds": 30,
    }


def test_build_pod_client_off_without_config_or_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMMA_TOKEN", raising=False)
    assert build_pod_client(EnvConfig.model_validate(_base_env())) is None  # no pod_sync
    env = EnvConfig.model_validate({**_base_env(), "pod_sync": {"pod_id": "p-1"}})
    assert build_pod_client(env) is None  # configured but no token


def test_build_pod_client_constructs_with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _FakePodCls:
        def __init__(self, *, pod_id: str, token: str, base_url: str, timeout: float) -> None:
            captured.update(pod_id=pod_id, token=token, base_url=base_url, timeout=timeout)

    monkeypatch.setattr("worker.pod_sync.Pod", _FakePodCls)
    monkeypatch.setenv("LEMMA_TOKEN", "tok-123")
    env = EnvConfig.model_validate(
        {**_base_env(), "pod_sync": {"pod_id": "p-9", "base_url": "https://api.x"}}
    )
    assert build_pod_client(env) is not None
    assert captured == {
        "pod_id": "p-9",
        "token": "tok-123",
        "base_url": "https://api.x",
        "timeout": 10.0,  # the PodSyncConfig default
    }


async def test_beat_recovers_from_a_stale_cached_row_id() -> None:
    # The cached row vanishes pod-side -> update raises -> the id is forgotten, so the next
    # beat re-finds/creates instead of wedging on a dead id until restart.
    pod = _FakePod()
    writer = _writer(pod)
    await writer.beat(now=NOW, armed=True, positions=[], detail={})  # creates + caches "row-1"
    assert writer._row_id == "row-1"
    pod.records._fail = True  # the row "disappears" -> update fails
    await writer.beat(now=NOW, armed=True, positions=[], detail={})  # swallowed; id forgotten
    assert writer._row_id is None
    pod.records._fail = False
    await writer.beat(now=NOW, armed=True, positions=[], detail={})  # re-creates
    assert len(pod.records.created) == 2


# --- PodCommandSource (pod -> worker) -------------------------------------------


def _cmd_src(pod: _FakePod) -> PodCommandSource:
    return PodCommandSource(cast(Pod, pod))


async def test_command_poll_maps_pending_commands() -> None:
    pod = _FakePod(
        pending=[
            {
                "id": "c1",
                "kind": "flatten",
                "worker_id": "alpha-paper-1",
                "deployment_id": None,
                "payload": {"x": 1},
            },
            {"id": "c2", "kind": "clear_halt", "worker_id": None, "deployment_id": "d9"},
        ]
    )
    cmds = await _cmd_src(pod).poll()
    assert [c.id for c in cmds] == ["c1", "c2"]
    assert cmds[0].kind is CommandKind.FLATTEN
    assert cmds[0].worker_id == "alpha-paper-1"
    assert cmds[0].payload == {"x": 1}
    assert cmds[1].kind is CommandKind.CLEAR_HALT
    assert cmds[1].worker_id is None  # a global command (the watcher applies it)


async def test_command_poll_returns_empty_on_pod_error() -> None:
    assert await _cmd_src(_FakePod(query_fail=True)).poll() == []  # best-effort read; no crash


async def test_command_poll_recovers_after_a_failure() -> None:
    pod = _FakePod(pending=[{"id": "c1", "kind": "flatten"}], query_fail=True)
    src = _cmd_src(pod)
    assert await src.poll() == []  # fails
    pod._query_fail = False
    assert [c.id for c in await src.poll()] == ["c1"]  # recovers


async def test_command_poll_skips_a_malformed_row() -> None:
    pod = _FakePod(pending=[{"id": "good", "kind": "flatten"}, {"id": "bad", "kind": "not_a_kind"}])
    cmds = await _cmd_src(pod).poll()
    assert [c.id for c in cmds] == ["good"]  # the unknown-kind row drops, not the whole batch


async def test_command_ack_updates_the_row() -> None:
    pod = _FakePod()
    await _cmd_src(pod).ack("c1", CommandStatus.DONE, detail="re-armed (clean reconcile)")
    table, record_id, data = pod.records.updated[0]
    assert table == "commands"
    assert record_id == "c1"
    assert data["status"] == "done"
    assert "acked_at" in data
    assert "detail" not in data  # the commands table has no detail column (logged, not stored)


async def test_command_ack_propagates_pod_errors() -> None:
    # ACK must raise on failure so the watcher retries, never executing an un-acked command.
    with pytest.raises(RuntimeError):
        await _cmd_src(_FakePod(fail=True)).ack("c1", CommandStatus.DONE)


def test_build_pod_client_swallows_construction_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(**_kw: Any) -> None:
        raise RuntimeError("bad base_url")

    monkeypatch.setattr("worker.pod_sync.Pod", _boom)
    monkeypatch.setenv("LEMMA_TOKEN", "tok")
    env = EnvConfig.model_validate({**_base_env(), "pod_sync": {"pod_id": "p-1"}})
    assert build_pod_client(env) is None  # a construction error -> None, never raises (TEST-8)


# --- token rotation (the relay keeps the .env fresh; the worker re-reads it) ----------


def test_build_pod_client_token_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rotation loop passes a freshly-read token; it overrides the (absent) startup env var."""
    monkeypatch.delenv("LEMMA_TOKEN", raising=False)
    env = EnvConfig.model_validate({**_base_env(), "pod_sync": {"pod_id": "p-1"}})
    assert build_pod_client(env) is None  # no startup token + no override -> off
    assert build_pod_client(env, token="fresh.jwt.value") is not None  # override builds a client


def test_read_envfile_token(tmp_path: Any) -> None:
    f = tmp_path / ".env"
    f.write_text(
        "# Alpha\nDELTA_TESTNET_API_KEY=abc\nLEMMA_TOKEN=eyJ.aGVhZA.sig\nOTHER=z\n",
        encoding="utf-8",
    )
    assert read_envfile_token(str(f), "LEMMA_TOKEN") == "eyJ.aGVhZA.sig"
    assert read_envfile_token(str(f), "MISSING") is None  # key absent -> None
    (tmp_path / "quoted.env").write_text('LEMMA_TOKEN="q.u.oted"\n', encoding="utf-8")
    assert read_envfile_token(str(tmp_path / "quoted.env"), "LEMMA_TOKEN") == "q.u.oted"
    (tmp_path / "empty.env").write_text("LEMMA_TOKEN=\n", encoding="utf-8")
    assert read_envfile_token(str(tmp_path / "empty.env"), "LEMMA_TOKEN") is None  # empty -> None
    assert (
        read_envfile_token(str(tmp_path / "nope.env"), "LEMMA_TOKEN") is None
    )  # missing file -> None


async def test_status_writer_set_pod_swaps_the_client() -> None:
    """A rotated client takes over writes; the cached row id is kept (same row, new client)."""
    old = _FakePod(existing=[{"id": "row-1", "worker_id": "w-1"}])
    writer = _writer(old)
    await writer.beat(now=NOW, armed=True, positions=[], detail={})  # caches row-1 on `old`
    new = _FakePod(existing=[{"id": "row-1", "worker_id": "w-1"}])
    writer.set_pod(cast(Pod, new))
    await writer.beat(now=NOW, armed=False, positions=[], detail={})
    assert len(new.records.updated) == 1  # the new client now serves writes
    assert len(old.records.updated) == 1  # the old client got only the first beat


async def test_command_source_set_pod_swaps_the_client() -> None:
    src = _cmd_src(_FakePod(pending=[]))  # old pod: nothing pending
    assert await src.poll() == []
    src.set_pod(cast(Pod, _FakePod(pending=[{"id": "c9", "kind": "start"}])))
    assert [c.id for c in await src.poll()] == ["c9"]  # the new client's commands now arrive
