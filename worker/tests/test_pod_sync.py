"""Pod-sync tests — the best-effort ``worker_status`` writer + the pod-client gate.

The writer must NEVER raise on a pod error (TEST-8: Lemma off the money path) and must
upsert by the unique ``worker_id`` (find-or-create, then update the cached row).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar, cast

import pytest
from lemma_sdk import Pod

from alpha_core.core.enums import AssetClass, OrderState, OrderType, Side, Venue
from alpha_core.core.models import Fill, Order, Position
from alpha_core.execution.commands import CommandKind, CommandStatus
from worker.config import EnvConfig
from worker.pod_sync import (
    PodCommandSource,
    PodStatusWriter,
    PodTradeSync,
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


async def test_record_risk_event_appends_a_row() -> None:
    pod = _FakePod()
    await _writer(pod).record_risk_event(
        kind="kill_tripped", severity="critical", now=NOW, detail={"trigger": "daily_loss"}
    )
    table, data = pod.records.created[0]
    assert table == "risk_events"
    assert data["kind"] == "kill_tripped"
    assert data["severity"] == "critical"
    assert data["ts"] == NOW.isoformat()
    assert data["detail"] == {"trigger": "daily_loss"}
    assert "deployment_id" not in data  # worker-scoped; the optional FK is omitted


async def test_record_risk_event_swallows_pod_errors() -> None:
    # TEST-8: a pod outage on the telemetry path must never raise into the worker.
    await _writer(_FakePod(fail=True)).record_risk_event(
        kind="kill_tripped", severity="critical", now=NOW, detail={}
    )  # must not raise


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
    (tmp_path / "pad.env").write_text("LEMMA_TOKEN=a.b.cc==\n", encoding="utf-8")
    assert read_envfile_token(str(tmp_path / "pad.env"), "LEMMA_TOKEN") == "a.b.cc=="  # `=` kept
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


# --- PodTradeSync (worker -> pod trade-coupled tables) ----------------------------


DEP = "0198c0de-0000-4000-8000-000000000001"  # a demo deployments.id (any valid UUID)


class _TradePod:
    """Stateful fake pod for the trade sync: real-ish tables with the unique keys the
    live pod enforces (client_order_id / dedup_key / position_key / snapshot_key), and a
    query router for the baseline SELECTs. ``fail=True`` = total outage (every call raises)."""

    _UNIQUE: ClassVar[dict[str, str]] = {
        "orders": "client_order_id",
        "fills": "dedup_key",
        "positions": "position_key",
        "pnl_snapshots": "snapshot_key",
    }

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.tables: dict[str, list[dict[str, Any]]] = {
            "orders": [],
            "fills": [],
            "positions": [],
            "pnl_snapshots": [],
        }
        self.creates = 0
        self.updates = 0
        self._next_id = 0
        self.records = self  # the SDK shape: pod.records.create/update

    def _rows(self, table: str) -> list[dict[str, Any]]:
        return self.tables[table]

    def create(self, table: str, data: dict[str, Any]) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("pod down")
        unique = self._UNIQUE[table]
        if any(r[unique] == data[unique] for r in self._rows(table)):
            raise RuntimeError(f"unique violation on {table}.{unique}")
        self._next_id += 1
        row = {**data, "id": f"{table}-{self._next_id}"}
        self._rows(table).append(row)
        self.creates += 1
        return row

    def update(self, table: str, record_id: str, data: dict[str, Any]) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("pod down")
        for row in self._rows(table):
            if row["id"] == record_id:
                row.update(data)
                self.updates += 1
                return row
        raise RuntimeError(f"no such row {record_id}")

    def query(self, sql: str) -> _FakeListResp:
        if self.fail:
            raise RuntimeError("pod down")
        if "FROM orders" in sql:
            return _FakeListResp(list(self.tables["orders"]))
        if "FROM positions" in sql:
            return _FakeListResp(list(self.tables["positions"]))
        if "FROM fills" in sql:
            return _FakeListResp(list(self.tables["fills"]))
        if "FROM pnl_snapshots" in sql:
            rows = sorted(self.tables["pnl_snapshots"], key=lambda r: str(r["ts"]), reverse=True)
            return _FakeListResp(rows[:1])
        raise AssertionError(f"unexpected query: {sql}")


def _order(client_order_id: str = "coid-1", *, state: str = "FILLED", filled: str = "1") -> Order:
    return Order(
        client_order_id=client_order_id,
        symbol="BTC/USD:USD",
        venue=Venue.DELTA,
        asset_class=AssetClass.CRYPTO,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        state=OrderState(state),
        filled_quantity=Decimal(filled),
        average_fill_price=Decimal("100") if Decimal(filled) > 0 else None,
        strategy_id="s-1",
        created_at=NOW,
        updated_at=NOW,
    )


def _fill(
    fill_id: str = "f-1", *, client_order_id: str = "coid-1", venue_fill_id: str | None = "vf-1"
) -> Fill:
    return Fill(
        fill_id=fill_id,
        client_order_id=client_order_id,
        venue_fill_id=venue_fill_id,
        symbol="BTC/USD:USD",
        venue=Venue.DELTA,
        asset_class=AssetClass.CRYPTO,
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("100"),
        fees=Decimal("0.05"),
        ts=NOW,
    )


def _held(qty: str = "1") -> Position:
    p = _pos("BTC/USD:USD", qty)
    if Decimal(qty) == 0:
        return p
    return p.model_copy(update={"realized_pnl": Decimal("2"), "last_price": Decimal("110")})


def _trade_sync(pod: _TradePod, *, snapshot_seconds: float = 300.0) -> PodTradeSync:
    return PodTradeSync(cast(Pod, pod), deployment_id=DEP, snapshot_seconds=snapshot_seconds)


async def _full_sync(sync: PodTradeSync, pod: _TradePod, **overrides: Any) -> None:
    kwargs: dict[str, Any] = {
        "now": NOW,
        "orders": [_order()],
        "fills": [_fill()],
        "positions": [_held()],
        "realized": Decimal("2"),
        "unrealized": Decimal("10"),
        "funding": Decimal("-0.01"),
    }
    kwargs.update(overrides)
    await sync.sync(**kwargs)


def test_trade_sync_rejects_a_non_uuid_deployment_id() -> None:
    with pytest.raises(ValueError):
        PodTradeSync(cast(Pod, _TradePod()), deployment_id="not-a-uuid", snapshot_seconds=300.0)


async def test_first_sync_creates_all_four_surfaces() -> None:
    pod = _TradePod()
    await _full_sync(_trade_sync(pod), pod)
    order_row = pod.tables["orders"][0]
    assert order_row["deployment_id"] == DEP
    assert order_row["client_order_id"] == "coid-1"
    assert order_row["state"] == "FILLED"
    assert order_row["quantity"] == "1"  # money/qty as str (B5)
    fill_row = pod.tables["fills"][0]
    assert fill_row["order_id"] == order_row["id"]  # the pod FK, not the local id
    assert fill_row["dedup_key"] == f"{order_row['id']}|vf-1"
    assert fill_row["price"] == "100"
    assert fill_row["ts"] == NOW.isoformat()
    pos_row = pod.tables["positions"][0]
    assert pos_row["position_key"] == f"{DEP}|DELTA|BTC/USD:USD"
    assert pos_row["quantity"] == "1"
    assert pos_row["unrealized_pnl"] == "10"  # (110-100)*1, computed from marks
    snap_row = pod.tables["pnl_snapshots"][0]
    assert snap_row["snapshot_key"].startswith(f"{DEP}|")
    assert snap_row["realized_pnl"] == "2"
    assert snap_row["fees_paid"] == "0.05"
    assert snap_row["funding_paid"] == "-0.01"
    assert snap_row["equity"] == "11.95"  # realized + unrealized - fees


async def test_second_sync_unchanged_writes_nothing() -> None:
    pod = _TradePod()
    sync = _trade_sync(pod)
    await _full_sync(sync, pod)
    creates, updates = pod.creates, pod.updates
    await _full_sync(sync, pod)  # identical book, same snapshot interval
    assert (pod.creates, pod.updates) == (creates, updates)


async def test_order_state_change_updates_in_place() -> None:
    pod = _TradePod()
    sync = _trade_sync(pod)
    await _full_sync(sync, pod, orders=[_order(state="PARTIALLY_FILLED", filled="1")])
    await _full_sync(sync, pod, orders=[_order(state="FILLED", filled="1")])
    assert len(pod.tables["orders"]) == 1  # updated, not duplicated
    assert pod.tables["orders"][0]["state"] == "FILLED"


async def test_restart_baselines_and_never_duplicates() -> None:
    pod = _TradePod()
    await _full_sync(_trade_sync(pod), pod)
    rows_before = {t: len(rows) for t, rows in pod.tables.items()}
    # A rebooted worker: fresh sync instance, same pod state, same local book.
    fresh = _trade_sync(pod)
    await _full_sync(fresh, pod)
    assert {t: len(rows) for t, rows in pod.tables.items()} == rows_before
    # A position CHANGE after the restart updates the baselined row in place.
    await _full_sync(fresh, pod, positions=[_held("3")])
    assert len(pod.tables["positions"]) == 1
    assert pod.tables["positions"][0]["quantity"] == "3"


async def test_fill_without_an_order_is_skipped_then_recovers() -> None:
    pod = _TradePod()
    sync = _trade_sync(pod)
    # The fill's order is not in the book (pruned) and not pod-side: skip, no crash, no row.
    await _full_sync(sync, pod, orders=[], fills=[_fill(client_order_id="ghost")])
    assert pod.tables["fills"] == []
    # Once the order appears, the fill lands on the next pass.
    await _full_sync(sync, pod, orders=[_order("ghost")], fills=[_fill(client_order_id="ghost")])
    assert len(pod.tables["fills"]) == 1


async def test_outage_never_raises_and_recovery_syncs_everything() -> None:
    pod = _TradePod(fail=True)
    sync = _trade_sync(pod)
    await _full_sync(sync, pod)  # total outage: must not raise (TEST-8)
    assert all(len(rows) == 0 for rows in pod.tables.values())
    pod.fail = False
    await _full_sync(sync, pod)  # recovery: nothing was falsely marked synced
    assert all(len(rows) == 1 for rows in pod.tables.values())


async def test_snapshot_cadence_and_flat_book_gate() -> None:
    pod = _TradePod()
    sync = _trade_sync(pod, snapshot_seconds=300.0)
    # A never-traded book writes no snapshot (idle soak must not fill the table).
    await _full_sync(
        sync, pod, orders=[], fills=[], positions=[], realized=Decimal(0), unrealized=Decimal(0)
    )
    assert pod.tables["pnl_snapshots"] == []
    await _full_sync(sync, pod)  # first active snapshot
    await _full_sync(sync, pod, now=NOW + timedelta(seconds=60))  # same 300s interval
    assert len(pod.tables["pnl_snapshots"]) == 1
    await _full_sync(sync, pod, now=NOW + timedelta(seconds=300))  # next interval
    assert len(pod.tables["pnl_snapshots"]) == 2
    # And the keys are the floored-interval isoformats (restart-stable).
    keys = {r["snapshot_key"] for r in pod.tables["pnl_snapshots"]}
    assert len(keys) == 2


async def test_restart_inside_an_interval_skips_the_existing_snapshot() -> None:
    pod = _TradePod()
    await _full_sync(_trade_sync(pod), pod)
    fresh = _trade_sync(pod)  # reboot: baseline seeds the snapshot cursor from the pod
    await _full_sync(fresh, pod)
    assert len(pod.tables["pnl_snapshots"]) == 1  # no collision warning, no duplicate


async def test_paper_fill_without_venue_fill_id_uses_the_local_fill_id() -> None:
    pod = _TradePod()
    await _full_sync(_trade_sync(pod), pod, fills=[_fill("local-7", venue_fill_id=None)])
    assert pod.tables["fills"][0]["dedup_key"].endswith("|local-7")


async def test_trade_sync_set_pod_swaps_the_client() -> None:
    old = _TradePod(fail=True)
    sync = _trade_sync(old)
    fresh = _TradePod()
    sync.set_pod(cast(Pod, fresh))
    await _full_sync(sync, fresh)
    assert len(fresh.tables["fills"]) == 1  # writes land on the swapped-in client


async def test_lost_create_ack_self_heals_by_rebaselining() -> None:
    # The create COMMITS pod-side but the response is lost (timeout/reset). The row must
    # not wedge into a forever-failing re-create: the failure schedules a re-baseline,
    # which finds the landed row and resumes updating it in place.
    pod = _TradePod()
    real_create = pod.create
    lost: list[str] = []

    def _lossy_create(table: str, data: dict[str, Any]) -> dict[str, Any]:
        row = real_create(table, data)
        if table == "orders" and not lost:  # first order create: commit, then "lose" the ack
            lost.append(row["id"])
            raise RuntimeError("response lost")
        return row

    pod.create = _lossy_create  # type: ignore[method-assign]
    sync = _trade_sync(pod)
    await _full_sync(sync, pod)  # order landed pod-side; ack lost; fill skipped (no FK yet)
    assert len(pod.tables["orders"]) == 1
    await _full_sync(sync, pod)  # re-baseline finds the row -> fill lands, no duplicate order
    assert len(pod.tables["orders"]) == 1
    assert len(pod.tables["fills"]) == 1
    assert pod.tables["fills"][0]["order_id"] == lost[0]
    # And a subsequent state change UPDATES the landed row (the id was adopted).
    await _full_sync(sync, pod, orders=[_order(state="CANCELLED", filled="1")])
    assert len(pod.tables["orders"]) == 1
    assert pod.tables["orders"][0]["state"] == "CANCELLED"


async def test_pod_side_deletion_self_heals_by_recreating() -> None:
    # An operator wipes the tables mid-run (demo reset): the cached ids go stale, the
    # updates fail once, the re-baseline forgets them, and the rows re-create.
    pod = _TradePod()
    sync = _trade_sync(pod)
    await _full_sync(sync, pod)
    for table in ("fills", "orders", "positions", "pnl_snapshots"):
        pod.tables[table].clear()  # pod-side wipe
    # A changed book forces writes against the now-dead cached ids -> fail -> re-baseline.
    await _full_sync(sync, pod, orders=[_order(state="CANCELLED")], positions=[_held("2")])
    await _full_sync(sync, pod, orders=[_order(state="CANCELLED")], positions=[_held("2")])
    assert len(pod.tables["orders"]) == 1  # re-created, not wedged on the stale id
    assert pod.tables["orders"][0]["state"] == "CANCELLED"
    assert len(pod.tables["positions"]) == 1
    assert pod.tables["positions"][0]["quantity"] == "2"
    assert len(pod.tables["fills"]) == 1  # re-created from the durable local history


async def test_degrade_and_recover_log_once_per_transition() -> None:
    # Finding-8 damping: a mid-life outage warns ONCE (trade_sync_degraded), row detail is
    # debug-level, and recovery logs once — never one warning per row per tick.
    pod = _TradePod()
    sync = _trade_sync(pod)
    await _full_sync(sync, pod)
    assert sync._sync_ok is True
    pod.fail = True
    await _full_sync(sync, pod, orders=[_order(state="CANCELLED")])  # rows fail -> degraded
    assert sync._sync_ok is False
    await _full_sync(sync, pod, orders=[_order(state="CANCELLED")])  # still down: no re-warn path
    assert sync._sync_ok is False
    pod.fail = False
    await _full_sync(sync, pod, orders=[_order(state="CANCELLED")])
    assert sync._sync_ok is True  # clean pass -> recovered (logged once)
