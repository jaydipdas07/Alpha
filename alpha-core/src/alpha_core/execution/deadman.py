"""Independent deadman — flatten a dead worker's book via broker REST (TEST-5, R1).

The deadman is a **separate process** (its own systemd unit, its own broker
adapter) that exists to satisfy one invariant: *a worker that dies mid-position
does not leave that position exposed*. It watches a liveness signal — the
worker's heartbeat file, plus an out-of-band ``emergency_flatten`` request — and
if the worker stops beating beyond the recovery-time objective (``rto_seconds``)
or an emergency is requested, it cancels every working order and closes every
open position with opposing market orders, **directly against broker truth**
(never through the worker's OMS, which may be the thing that died).

Design rules:
- **Independent.** It shares no in-process state with the worker; its only inputs
  are the broker (truth) and the liveness signal. It must survive its own restart.
- **Resilient.** ``run`` never dies on a transient broker error — it logs and
  retries next poll; the deadman is the last line of defence.
- **Idempotent.** Within one trip *episode* a position's flatten order carries a
  stable client id, so re-issuing it across polls is a venue-side no-op (dedup by
  client-order-id) — it never stacks duplicate closes. A new episode (a fresh
  trip after recovery) gets a fresh token, so a later position is still flattened.
- **Money is Decimal; time is tz-aware UTC injected via the clock.** No broker SDK
  is imported here — the deadman talks only to the ``BrokerAdapter`` contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from alpha_core.core.enums import OrderState, OrderType, Side, Venue
from alpha_core.core.interfaces import BrokerAdapter
from alpha_core.core.models import Order, Position
from alpha_core.helpers.config import load_yaml
from alpha_core.observability.logging import get_logger
from alpha_core.observability.notify import LoggingNotifier, Notifier, Severity
from alpha_core.scheduler.clock import Clock, SystemClock


@runtime_checkable
class LivenessSource(Protocol):
    """The worker-liveness signal the deadman watches (injected, testable)."""

    def last_beat(self) -> datetime | None:
        """The worker's last heartbeat (tz-aware UTC), or None if it never beat."""
        ...

    def flatten_requested(self) -> bool:
        """True iff an out-of-band emergency flatten has been requested."""
        ...


class HeartbeatFile:
    """A heartbeat written by the worker and read by the deadman (both sides).

    The worker calls ``beat(now)`` each loop; the deadman reads ``last_beat()``.
    A local file is the authoritative liveness signal — it needs no network and
    keeps the pod off the safety path (TEST-8). The write is atomic (temp +
    ``os.replace``) so the deadman never reads a half-written timestamp. As a
    ``LivenessSource`` it reports no emergency (that arrives via another source)."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    def beat(self, now: datetime) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(now.astimezone(UTC).isoformat())
        os.replace(tmp, self._path)

    def last_beat(self) -> datetime | None:
        try:
            raw = self._path.read_text().strip()
        except (FileNotFoundError, OSError):
            return None
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)

    def flatten_requested(self) -> bool:
        return False


class DeadmanConfig(BaseModel):
    """Deadman budgets (``config/worker.yaml`` ``deadman:`` section). No magic numbers."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    # If the worker hasn't beaten within this many seconds it is declared dead.
    rto_seconds: float = Field(gt=0)
    # How often the deadman re-checks liveness — must be <= rto so it reacts in time.
    poll_interval_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def _check_poll_within_rto(self) -> DeadmanConfig:
        if self.poll_interval_seconds > self.rto_seconds:
            raise ValueError(
                "poll_interval_seconds must be <= rto_seconds (else it can't react in time)"
            )
        return self


def load_deadman_config() -> DeadmanConfig:
    """Load + validate the ``deadman:`` section of ``config/worker.yaml``."""
    return DeadmanConfig.model_validate(load_yaml("worker.yaml").get("deadman", {}))


@dataclass(frozen=True, slots=True)
class DeadmanReport:
    """The outcome of one deadman check."""

    tripped: bool
    reason: str | None = None
    first_trip: bool = False
    cancelled: list[str] = field(default_factory=list)
    flattened: list[Order] = field(default_factory=list)


def _flatten_id(pos: Position, venue: Venue, episode: str) -> str:
    """Stable client id for closing ``pos`` within trip ``episode`` (idempotent).

    Keyed by (symbol, venue, signed qty, episode): identical across polls of the
    same residual in the same episode (venue dedups the re-issue), distinct for a
    different residual or a fresh episode (so a later position is still flattened)."""
    parts = "|".join(str(x) for x in ("deadman", pos.symbol, venue.value, pos.quantity, episode))
    return f"alpha-{hashlib.sha1(parts.encode()).hexdigest()[:16]}"


class Deadman:
    """Watches worker liveness; flattens the book via broker REST on death (TEST-5)."""

    def __init__(
        self,
        *,
        adapter: BrokerAdapter,
        liveness: LivenessSource,
        config: DeadmanConfig,
        venue: Venue,
        clock: Clock | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self._adapter = adapter
        self._liveness = liveness
        self._cfg = config
        self._venue = venue
        self._clock = clock or SystemClock()
        self._notifier = notifier or LoggingNotifier()
        self._log = get_logger("deadman")
        self._tripped = False
        self._episode: str | None = None
        self._started = self._clock.now()

    def _trip_reason(self, now: datetime) -> str | None:
        """Why the deadman should flatten now, or None if the worker is healthy."""
        if self._liveness.flatten_requested():
            return "emergency_flatten requested"
        last = self._liveness.last_beat()
        if last is None:
            # No beat yet: tolerate one RTO of grace from the deadman's own start,
            # so a cold start before the worker's first beat doesn't false-trip.
            if (now - self._started).total_seconds() > self._cfg.rto_seconds:
                return "no worker heartbeat since deadman start"
            return None
        age = (now - last).total_seconds()
        if age > self._cfg.rto_seconds:
            return f"worker heartbeat stale ({age:.1f}s > rto {self._cfg.rto_seconds}s)"
        return None

    async def check(self) -> DeadmanReport:
        """One liveness check; flatten the book if the worker is dead."""
        now = self._clock.now()
        reason = self._trip_reason(now)
        if reason is None:
            if self._tripped:
                self._log.info("deadman_recovered")
                self._notifier.send(
                    "deadman: worker recovered, releasing", severity=Severity.WARNING
                )
            self._tripped = False
            self._episode = None
            return DeadmanReport(tripped=False)

        first_trip = not self._tripped
        if first_trip:
            self._episode = now.isoformat()
            self._log.error("deadman_trip", reason=reason)
            self._notifier.send(
                f"DEADMAN TRIP: {reason} — flattening book", severity=Severity.CRITICAL
            )
        self._tripped = True

        cancelled = await self._adapter.cancel_all()
        flattened = await self._flatten_positions(now)
        if cancelled or flattened:
            self._log.warning(
                "deadman_flatten",
                reason=reason,
                cancelled=len(cancelled),
                flattened=[o.symbol for o in flattened],
            )
        return DeadmanReport(
            tripped=True,
            reason=reason,
            first_trip=first_trip,
            cancelled=cancelled,
            flattened=flattened,
        )

    async def _flatten_positions(self, now: datetime) -> list[Order]:
        """Close every open position with an opposing market order (broker truth)."""
        assert self._episode is not None  # set on trip before this is called
        placed: list[Order] = []
        for pos in await self._adapter.get_positions():
            if pos.quantity == 0:
                continue
            side = Side.SELL if pos.quantity > 0 else Side.BUY
            order = Order(
                client_order_id=_flatten_id(pos, self._venue, self._episode),
                symbol=pos.symbol,
                venue=self._venue,
                asset_class=pos.asset_class,
                side=side,
                order_type=OrderType.MARKET,
                quantity=abs(pos.quantity),
                state=OrderState.NEW,
                strategy_id="deadman",
                created_at=now,
                updated_at=now,
            )
            await self._adapter.place_order(order)
            placed.append(order)
        return placed

    async def run(self) -> None:
        """Poll liveness forever; never die on a transient error (last line of defence)."""
        self._log.info(
            "deadman_start",
            venue=self._venue.value,
            rto_seconds=self._cfg.rto_seconds,
            poll_seconds=self._cfg.poll_interval_seconds,
        )
        while True:
            try:
                await self.check()
            except Exception as exc:  # the deadman must never crash on a poll error
                self._log.error("deadman_check_error", error=str(exc))
            await asyncio.sleep(self._cfg.poll_interval_seconds)
