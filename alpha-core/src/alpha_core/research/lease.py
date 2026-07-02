"""Heartbeat staleness + backtest work-leases so a dead research box never hangs (R8).

A backtest request is *leased* by the research box that claims it; the lease carries a
hard expiry (``acquired_at + ttl``). If the box dies mid-run, the lease lapses and the
request becomes reclaimable — it can never block a workflow forever. Likewise a
heartbeat older than the timeout is *stale* and alertable. All time is tz-aware UTC and
``now`` is **injected** (never wall-clock), so this is deterministic in backtest and
live. Thresholds live in ``config/research.yaml`` (no magic numbers).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from alpha_core.helpers.config import load_yaml
from alpha_core.research.tripwire import TripwireConfig


class ResearchConfig(BaseModel):
    """Research-box heartbeat + backtest-lease thresholds (``config/research.yaml``)."""

    model_config = ConfigDict(extra="forbid")
    heartbeat_timeout_seconds: int = 120
    lease_ttl_seconds: int = 1800
    # the funding-regime tripwire monitor (scripts/funding_tripwire.py; research/tripwire.py)
    funding_tripwire: TripwireConfig = Field(default_factory=TripwireConfig)

    @property
    def heartbeat_timeout(self) -> timedelta:
        return timedelta(seconds=self.heartbeat_timeout_seconds)

    @property
    def lease_ttl(self) -> timedelta:
        return timedelta(seconds=self.lease_ttl_seconds)

    @classmethod
    def from_config(cls) -> ResearchConfig:
        """Load from ``config/research.yaml`` (no magic numbers)."""
        return cls.model_validate(load_yaml("research.yaml"))


def is_stale(last_seen: datetime, now: datetime, timeout: timedelta) -> bool:
    """True when the last heartbeat is older than ``timeout`` (alert + reclaim its work).

    ``last_seen`` and ``now`` are tz-aware UTC; ``now`` is injected so staleness is
    deterministic in backtest and live.
    """
    if last_seen.tzinfo is None or now.tzinfo is None:
        raise ValueError("is_stale timestamps must be tz-aware UTC")
    # strict ``>``: at exactly ``timeout`` not yet stale (wait the full window before
    # alerting) — contrast ``Lease.is_expired``'s ``>=`` (eager reclaim at the deadline).
    return now - last_seen > timeout


@dataclass(frozen=True)
class Lease:
    """A time-bounded claim on a unit of work (e.g. a backtest request).

    The expiry is the whole point: if the holder dies mid-run the lease lapses and the
    work is reclaimable — it never hangs a workflow forever (R8).
    """

    holder: str
    acquired_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.acquired_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("lease timestamps must be tz-aware UTC")

    def is_expired(self, now: datetime) -> bool:
        """True once ``now`` reaches the expiry — the lease is then reclaimable."""
        return now >= self.expires_at  # ``>=``: reclaim eagerly at the deadline


def acquire(current: Lease | None, holder: str, now: datetime, ttl: timedelta) -> Lease | None:
    """Claim the lease, or return ``None`` if a *different, live* holder owns it.

    Free (``None``), expired, or already-ours leases are (re)acquired; only an unexpired
    lease held by someone else blocks — and that one expires on its own, so a dead holder
    can never block the work permanently.
    """
    if current is not None and current.holder != holder and not current.is_expired(now):
        return None
    return Lease(holder=holder, acquired_at=now, expires_at=now + ttl)


def renew(lease: Lease, now: datetime, ttl: timedelta) -> Lease:
    """Extend a lease the caller already holds (call while the work is still alive)."""
    return Lease(holder=lease.holder, acquired_at=lease.acquired_at, expires_at=now + ttl)
