"""Pod-sync — best-effort worker → pod telemetry (M3.4/M3.6).

The worker is the sole executor (TEST-8); the pod (Vault) is mission control. This
writes a ``worker_status`` heartbeat so the cockpit sees the worker live/armed. It is
**strictly best-effort**: every pod call swallows errors (logs + returns), so a pod
outage is never on the worker's path — it cannot stall trading, the kill-switch, the
deadman, or reconcile (Lemma is never on the money path; TEST-8).

The pod SDK (``lemma_sdk``) is the WORKER's dependency, never ``alpha-core``'s (the
kernel imports no SDK — same rule as ccxt). The SDK is synchronous, so every call runs
in ``asyncio.to_thread`` to keep the event loop responsive.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from lemma_sdk import Pod

from alpha_core.core.models import Fill, Order, Position
from alpha_core.execution.commands import Command, CommandKind, CommandStatus
from alpha_core.observability.logging import get_logger
from worker.config import EnvConfig

_log = get_logger("pod_sync")


def build_pod_client(env: EnvConfig, *, token: str | None = None) -> Pod | None:
    """Construct the Vault-pod client from ``env.pod_sync`` + a token, or ``None`` if pod-sync
    is unconfigured/disabled or no token is present. ``token`` overrides the startup
    ``$token_env`` (the rotation loop passes a freshly-read token). Never raises — pod-sync is
    best-effort, so a missing/bad token just means "no pod telemetry"."""
    cfg = env.pod_sync
    if cfg is None or not cfg.enabled:
        return None
    tok = token if token is not None else os.environ.get(cfg.token_env, "")
    if not tok:
        _log.info("pod_sync_disabled", reason=f"no {cfg.token_env} in environment")
        return None
    try:
        return Pod(pod_id=cfg.pod_id, token=tok, base_url=cfg.base_url, timeout=cfg.timeout_seconds)
    except Exception as exc:
        _log.warning("pod_client_build_failed", error=str(exc))
        return None


def read_envfile_token(path: str, key: str) -> str | None:
    """Read ``key``'s value from a ``.env``-style file (the value the relay keeps fresh), or
    ``None`` if the file/key is absent. The rotation loop must read the FILE — ``os.environ`` is
    frozen at startup (``load_dotenv`` uses ``setdefault``). Best-effort: a missing/unreadable file
    returns ``None`` (rotation just doesn't happen this tick)."""
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line.startswith(f"{key}="):
                    return line[len(key) + 1 :].strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None


def positions_hash(positions: list[Position]) -> str:
    """A stable short hash of the open book (venue:symbol:qty) so the cockpit can spot
    a position change without shipping the full book on every heartbeat."""
    parts = sorted(f"{p.venue.value}:{p.symbol}:{p.quantity}" for p in positions if p.quantity != 0)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


class PodStatusWriter:
    """Upserts the worker's ``worker_status`` heartbeat row (one row per ``worker_id``).

    BEST-EFFORT: ``beat`` swallows every pod error (logs + returns) so a pod outage never
    touches the worker loop. The row id is discovered/cached on first write — the table
    keys on a unique ``worker_id`` but the SDK updates by record id, so we find-or-create.
    """

    def __init__(
        self, pod: Pod, *, worker_id: str, mode: str, build_version: str | None = None
    ) -> None:
        self._pod = pod
        self._worker_id = worker_id
        self._mode = mode
        self._build_version = build_version
        self._row_id: str | None = None

    def set_pod(self, pod: Pod) -> None:
        """Swap in a freshly-tokened pod client (token rotation). Keeps the cached ``_row_id`` —
        it is the same row, only the client changed. Reference assignment is atomic (GIL), so a
        concurrent ``beat`` in another thread uses one consistent client, never a torn state."""
        self._pod = pod

    async def beat(
        self,
        *,
        now: datetime,
        armed: bool,
        positions: list[Position],
        detail: dict[str, Any],
    ) -> None:
        """Upsert the heartbeat row. ``armed`` mirrors the latching kill-switch (armed =
        NOT halted). Never raises — a pod hiccup is logged and ignored (TEST-8)."""
        data: dict[str, Any] = {
            "worker_id": self._worker_id,
            "last_seen": now.isoformat(),
            "mode": self._mode,
            "armed": armed,
            "positions_hash": positions_hash(positions),
            "build_version": self._build_version,
            "detail": detail,
        }
        try:
            await asyncio.to_thread(self._upsert, data)
        except Exception as exc:
            _log.warning("worker_status_beat_failed", error=str(exc))

    async def record_risk_event(
        self, *, kind: str, severity: str, now: datetime, detail: dict[str, Any]
    ) -> None:
        """Append one ``risk_events`` row so mission control sees a worker risk event (e.g. a
        kill-switch trip). Append-only; no ``deployment_id`` (worker-scoped — optional column).
        BEST-EFFORT (TEST-8): a pod hiccup is logged and ignored — never raised, and the caller is
        the pod-sync loop (off the money path), so this can't touch trading or the kill-switch."""
        data: dict[str, Any] = {
            "kind": kind,
            "severity": severity,
            "ts": now.isoformat(),
            "detail": detail,
        }
        try:
            await asyncio.to_thread(self._pod.records.create, "risk_events", data)
        except Exception as exc:
            _log.warning("risk_event_sync_failed", error=str(exc), kind=kind)

    def _upsert(self, data: dict[str, Any]) -> None:
        if self._row_id is None:
            self._row_id = self._find_row_id()
        if self._row_id is None:
            rec = self._pod.records.create("worker_status", data)
            self._row_id = str(rec["id"]) if isinstance(rec, dict) and "id" in rec else None
            return
        try:
            self._pod.records.update("worker_status", self._row_id, data)
        except Exception:
            # The cached row went stale (deleted pod-side) — forget it so the next beat
            # re-finds or re-creates, rather than wedging on a dead id until restart.
            self._row_id = None
            raise

    def _find_row_id(self) -> str | None:
        # worker_status is one row per worker (low volume) — list + match in Python, no
        # SQL interpolation of the worker_id.
        resp = self._pod.records.list("worker_status", limit=100)
        for item in resp.to_dict().get("items", []):
            if isinstance(item, dict) and item.get("worker_id") == self._worker_id:
                return str(item["id"])
        return None


def _opt(value: object) -> str | None:
    """String-encode an optional Decimal for a pod TEXT money column (B5) — never a float."""
    return None if value is None else str(value)


class PodTradeSync:
    """Per-event worker → pod sync of the trade-coupled tables (M3.4 deferred increment):
    ``orders`` (upsert by unique ``client_order_id``), ``fills`` (append-only, idempotent by
    unique ``dedup_key``), ``positions`` (upsert by unique ``position_key``) and cadenced
    ``pnl_snapshots`` (insert by unique ``snapshot_key``) — so mission control charts the
    deployment without ever being on the money path.

    STRICTLY BEST-EFFORT (TEST-8): ``sync`` swallows every pod error (logs + returns) and is
    called from the telemetry loop, never the trading path — a pod outage costs telemetry
    freshness only, and every unsynced row simply retries next tick (nothing is marked synced
    until the pod write succeeded). Restart-safe: the first sync BASELINES from the pod (row
    ids by their unique keys + already-present fill ``dedup_key``s), so a rebooted worker
    updates existing rows instead of duplicating them. All money is string-Decimal (B5).
    """

    def __init__(self, pod: Pod, *, deployment_id: str, snapshot_seconds: float) -> None:
        # The id is interpolated into the baseline SELECTs — validate it is a real UUID
        # (config-supplied, but never trust an interpolated value's shape).
        self._deployment_id = str(uuid.UUID(deployment_id))
        self._snapshot_seconds = snapshot_seconds
        self._pod = pod
        self._baselined = False
        self._order_ids: dict[str, str] = {}  # client_order_id -> pod orders.id
        self._order_fps: dict[str, tuple[str, str, str | None, str | None]] = {}
        self._position_ids: dict[str, str] = {}  # position_key -> pod positions.id
        self._position_fps: dict[str, tuple[str, str | None, str, str | None]] = {}
        self._pod_dedup_keys: set[str] = set()  # fill dedup_keys confirmed pod-side
        self._last_snapshot_key: str | None = None

    def set_pod(self, pod: Pod) -> None:
        """Swap in a freshly-tokened pod client (rotation; see ``PodStatusWriter.set_pod``)."""
        self._pod = pod

    async def sync(
        self,
        *,
        now: datetime,
        orders: list[Order],
        fills: list[Fill],
        positions: list[Position],
        realized: Decimal,
        unrealized: Decimal,
        funding: Decimal,
    ) -> None:
        """One best-effort sync pass over the worker's current book + fill history (the
        caller reads fills from the durable store — the derived-P&L truth, R11). Never
        raises; a failed surface just retries next tick."""
        try:
            await asyncio.to_thread(
                self._sync_blocking, now, orders, fills, positions, realized, unrealized, funding
            )
        except Exception as exc:  # TEST-8: telemetry must never touch the worker's path
            _log.warning("trade_sync_failed", error=str(exc))

    def _sync_blocking(
        self,
        now: datetime,
        orders: list[Order],
        fills: list[Fill],
        positions: list[Position],
        realized: Decimal,
        unrealized: Decimal,
        funding: Decimal,
    ) -> None:
        if not self._baselined:
            self._baseline()  # raises on pod failure -> whole pass retries next tick
        # Orders FIRST: fills FK the pod orders row, so its id must exist before any fill.
        self._sync_orders(orders)
        self._sync_fills(fills)
        self._sync_positions(positions)
        fees = sum((f.fees for f in fills if f.fees is not None), Decimal(0))
        self._maybe_snapshot(
            now, fills=fills, realized=realized, unrealized=unrealized, funding=funding, fees=fees
        )

    def _baseline(self) -> None:
        """Load this deployment's existing pod rows ONCE (restart recovery): row ids by
        their unique keys, and the fill ``dedup_key``s already present, so re-syncing a
        rebuilt local book updates in place instead of duplicating. The deployment id is
        UUID-validated at construction, so the interpolation below cannot inject."""
        dep = self._deployment_id
        for item in self._query(
            f"SELECT id, client_order_id FROM orders WHERE deployment_id = '{dep}'"
        ):
            self._order_ids[str(item["client_order_id"])] = str(item["id"])
        for item in self._query(
            f"SELECT id, position_key FROM positions WHERE deployment_id = '{dep}'"
        ):
            self._position_ids[str(item["position_key"])] = str(item["id"])
        for item in self._query(
            "SELECT f.dedup_key AS dedup_key FROM fills f JOIN orders o ON f.order_id = o.id "
            f"WHERE o.deployment_id = '{dep}'"
        ):
            self._pod_dedup_keys.add(str(item["dedup_key"]))
        try:
            # Seed the snapshot cursor so a restart inside an interval skips it instead of
            # colliding on the unique key. Best-effort ONLY (its own try): if the pod's SQL
            # dialect rejects ORDER BY/LIMIT the cost is one logged collision, not a
            # baseline that can never complete.
            latest = self._query(
                "SELECT snapshot_key FROM pnl_snapshots "
                f"WHERE deployment_id = '{dep}' ORDER BY ts DESC LIMIT 1"
            )
            if latest:
                self._last_snapshot_key = str(latest[0]["snapshot_key"])
        except Exception as exc:
            _log.warning("snapshot_cursor_seed_failed", error=str(exc))
        self._baselined = True
        _log.info(
            "trade_sync_baselined",
            orders=len(self._order_ids),
            positions=len(self._position_ids),
            fills=len(self._pod_dedup_keys),
        )

    def _query(self, sql: str) -> list[dict[str, Any]]:
        resp = self._pod.query(sql)
        return [it for it in resp.to_dict().get("items", []) if isinstance(it, dict)]

    def _sync_orders(self, orders: list[Order]) -> None:
        for order in orders:
            fp = (
                order.state.value,
                str(order.filled_quantity),
                _opt(order.average_fill_price),
                order.venue_order_id,
            )
            if self._order_fps.get(order.client_order_id) == fp:
                continue  # unchanged since the last successful sync
            data: dict[str, Any] = {
                "deployment_id": self._deployment_id,
                "client_order_id": order.client_order_id,
                "venue_order_id": order.venue_order_id,
                "symbol": order.symbol,
                "venue": order.venue.value,
                "asset_class": order.asset_class.value,
                "side": order.side.value,
                "order_type": order.order_type.value,
                "state": order.state.value,
                "quantity": str(order.quantity),
                "limit_price": _opt(order.limit_price),
                "stop_price": _opt(order.stop_price),
                "filled_quantity": str(order.filled_quantity),
                "average_fill_price": _opt(order.average_fill_price),
            }
            try:
                row_id = self._order_ids.get(order.client_order_id)
                if row_id is None:
                    rec = self._pod.records.create("orders", data)
                    if isinstance(rec, dict) and "id" in rec:
                        self._order_ids[order.client_order_id] = str(rec["id"])
                else:
                    self._pod.records.update("orders", row_id, data)
                self._order_fps[order.client_order_id] = fp  # only after the pod write stuck
            except Exception as exc:  # per-row: one bad order must not block the rest
                _log.warning(
                    "order_sync_failed", client_order_id=order.client_order_id, error=str(exc)
                )

    def _sync_fills(self, fills: list[Fill]) -> None:
        for fill in fills:
            pod_order_id = self._order_ids.get(fill.client_order_id)
            if pod_order_id is None:
                # Its order didn't sync (pod hiccup, or a book pruned before first sync) —
                # retry next tick once the order row exists; never fabricate the FK.
                _log.warning("fill_sync_no_order", client_order_id=fill.client_order_id)
                continue
            # venue_fill_id when the venue gave one; else the local fill_id (client-generated,
            # persisted, restart-stable) — either way the key survives a worker rebuild.
            dedup_key = f"{pod_order_id}|{fill.venue_fill_id or fill.fill_id}"
            if dedup_key in self._pod_dedup_keys:
                continue
            data: dict[str, Any] = {
                "order_id": pod_order_id,
                "dedup_key": dedup_key,
                "venue_order_id": fill.venue_order_id,
                "venue_fill_id": fill.venue_fill_id,
                "symbol": fill.symbol,
                "venue": fill.venue.value,
                "side": fill.side.value,
                "quantity": str(fill.quantity),
                "price": str(fill.price),
                "fees": _opt(fill.fees),
                "ts": fill.ts.isoformat(),
            }
            try:
                self._pod.records.create("fills", data)
                self._pod_dedup_keys.add(dedup_key)  # only after the pod accepted it
            except Exception as exc:
                _log.warning("fill_sync_failed", dedup_key=dedup_key, error=str(exc))

    def _sync_positions(self, positions: list[Position]) -> None:
        for position in positions:
            key = f"{self._deployment_id}|{position.venue.value}|{position.symbol}"
            unrealized: Decimal | None = None
            if (
                position.quantity != 0
                and position.average_price is not None
                and position.last_price is not None
            ):
                unrealized = (position.last_price - position.average_price) * position.quantity
            fp = (
                str(position.quantity),
                _opt(position.average_price),
                str(position.realized_pnl),
                _opt(unrealized),
            )
            if self._position_fps.get(key) == fp:
                continue
            data: dict[str, Any] = {
                "deployment_id": self._deployment_id,
                "position_key": key,
                "symbol": position.symbol,
                "venue": position.venue.value,
                "quantity": str(position.quantity),  # signed; "0" = flat (a state, kept updated)
                "avg_entry_price": _opt(position.average_price),
                "realized_pnl": str(position.realized_pnl),
                "unrealized_pnl": _opt(unrealized),
            }
            try:
                row_id = self._position_ids.get(key)
                if row_id is None:
                    rec = self._pod.records.create("positions", data)
                    if isinstance(rec, dict) and "id" in rec:
                        self._position_ids[key] = str(rec["id"])
                else:
                    self._pod.records.update("positions", row_id, data)
                self._position_fps[key] = fp
            except Exception as exc:
                _log.warning("position_sync_failed", position_key=key, error=str(exc))

    def _maybe_snapshot(
        self,
        now: datetime,
        *,
        fills: list[Fill],
        realized: Decimal,
        unrealized: Decimal,
        funding: Decimal,
        fees: Decimal,
    ) -> None:
        """One ``pnl_snapshots`` row per ``snapshot_seconds`` interval (key = the epoch-floored
        ts; the baseline seeds the cursor so a restart inside an interval skips, and the unique
        ``snapshot_key`` is the pod-side backstop either way). Skipped while the book has never
        traded (no fills, zero P&L) — an idle soak must not fill the table with flat points."""
        if not fills and realized == 0 and unrealized == 0:
            return
        epoch = now.astimezone(UTC).timestamp()
        floored = datetime.fromtimestamp(
            (epoch // self._snapshot_seconds) * self._snapshot_seconds, tz=UTC
        )
        key = f"{self._deployment_id}|{floored.isoformat()}"
        if key == self._last_snapshot_key:
            return
        data: dict[str, Any] = {
            "deployment_id": self._deployment_id,
            "snapshot_key": key,
            "ts": floored.isoformat(),
            "realized_pnl": str(realized),  # positions fold + funding (gross of fees)
            "unrealized_pnl": str(unrealized),
            "equity": str(realized + unrealized - fees),  # net-of-costs P&L equity (base 0)
            "fees_paid": str(fees),
            "funding_paid": str(funding),
        }
        try:
            self._pod.records.create("pnl_snapshots", data)
            self._last_snapshot_key = key
        except Exception as exc:
            # NOT claimed: a pod outage retries next tick and the interval's point is kept.
            # (A unique-key collision would retry-warn until the interval rolls — but the
            # baseline seeds the cursor, so collisions need a failed seed AND a restart.)
            _log.warning("pnl_snapshot_sync_failed", snapshot_key=key, error=str(exc))


def _to_command(item: dict[str, Any]) -> Command:
    """Map a pod ``commands`` row to the kernel's ``Command`` (kind from the table enum)."""
    return Command(
        id=str(item["id"]),
        kind=CommandKind(item["kind"]),
        worker_id=item.get("worker_id"),
        deployment_id=item.get("deployment_id"),
        payload=item.get("payload"),
    )


class PodCommandSource:
    """Pod-backed ``CommandSource`` — the cockpit's governance commands reach the worker.

    The cockpit issues commands (arm/flatten/halt/start/stop) into the pod ``commands``
    table; this polls the PENDING ones for the worker's ``CommandWatcher`` and acks them
    back. TEST-8: the pod only ISSUES commands — the worker is the sole executor; this is
    just the read/ack adapter, never a trading path.

    READ is best-effort: a pod outage => ``poll`` returns ``[]`` (the worker keeps running
    per its local state; commands arrive when the pod returns). ACK PROPAGATES on failure
    so the watcher never executes a command it couldn't first mark acked (no double-fire).
    """

    def __init__(self, pod: Pod) -> None:
        self._pod = pod
        self._poll_ok = True

    def set_pod(self, pod: Pod) -> None:
        """Swap in a freshly-tokened pod client (rotation; see ``PodStatusWriter.set_pod``)."""
        self._pod = pod

    async def poll(self) -> list[Command]:
        try:
            items = await asyncio.to_thread(self._fetch_pending)
        except Exception as exc:  # best-effort read: surface once, keep the worker running
            if self._poll_ok:
                _log.warning("command_poll_failed", error=str(exc))
                self._poll_ok = False
            return []
        if not self._poll_ok:
            _log.info("command_poll_recovered")
            self._poll_ok = True
        commands: list[Command] = []
        for item in items:
            try:
                commands.append(_to_command(item))
            except Exception as exc:  # one malformed row must not drop the whole batch
                _log.warning("command_parse_skipped", error=str(exc), row=item.get("id"))
        return commands

    def _fetch_pending(self) -> list[dict[str, Any]]:
        resp = self._pod.query(
            "SELECT id, kind, worker_id, deployment_id, payload FROM commands "
            "WHERE status = 'pending'"
        )
        return [it for it in resp.to_dict().get("items", []) if isinstance(it, dict)]

    async def ack(
        self, command_id: str, status: CommandStatus, *, detail: str | None = None
    ) -> None:
        """Move the command to ``status`` + stamp ``acked_at``. Propagates a pod error so
        the watcher retries rather than executing a command it couldn't mark (the
        ``commands`` table has no detail column, so ``detail`` is logged, not stored)."""
        if detail is not None:
            _log.info("command_ack", command_id=command_id, status=status.value, detail=detail)
        data: dict[str, Any] = {"status": status.value, "acked_at": datetime.now(UTC).isoformat()}
        await asyncio.to_thread(self._pod.records.update, "commands", command_id, data)
