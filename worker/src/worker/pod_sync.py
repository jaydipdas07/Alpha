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
from datetime import UTC, datetime
from typing import Any

from lemma_sdk import Pod

from alpha_core.core.models import Position
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
