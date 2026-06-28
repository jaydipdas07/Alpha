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
from datetime import datetime
from typing import Any

from lemma_sdk import Pod

from alpha_core.core.models import Position
from alpha_core.observability.logging import get_logger
from worker.config import EnvConfig

_log = get_logger("pod_sync")


def build_pod_client(env: EnvConfig) -> Pod | None:
    """Construct the Vault-pod client from ``env.pod_sync`` + the staged token, or
    ``None`` if pod-sync is unconfigured/disabled or no token is present. Never raises —
    pod-sync is best-effort, so a missing token just means "no pod telemetry"."""
    cfg = env.pod_sync
    if cfg is None or not cfg.enabled:
        return None
    token = os.environ.get(cfg.token_env, "")
    if not token:
        _log.info("pod_sync_disabled", reason=f"no {cfg.token_env} in environment")
        return None
    try:
        return Pod(pod_id=cfg.pod_id, token=token, base_url=cfg.base_url)
    except Exception as exc:
        _log.warning("pod_client_build_failed", error=str(exc))
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
        else:
            self._pod.records.update("worker_status", self._row_id, data)

    def _find_row_id(self) -> str | None:
        # worker_status is one row per worker (low volume) — list + match in Python, no
        # SQL interpolation of the worker_id.
        resp = self._pod.records.list("worker_status", limit=100)
        for item in resp.to_dict().get("items", []):
            if isinstance(item, dict) and item.get("worker_id") == self._worker_id:
                return str(item["id"])
        return None
