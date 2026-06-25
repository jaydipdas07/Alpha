"""Proactive rate limiter (ADR 0004).

Keeps outbound requests under a venue's API cap *before* a 429 happens (distinct
from ``risk.max_orders_per_minute``, which is a risk-layer control). A simple
sliding-window limiter: at most ``max_events`` within ``per_seconds``.

The clock is injectable so tests are deterministic and don't sleep in real time.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable


class RateLimiter:
    """Async sliding-window limiter: ``max_events`` per ``per_seconds``."""

    def __init__(
        self,
        max_events: int,
        per_seconds: float,
        *,
        now: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events must be >= 1")
        if per_seconds <= 0:
            raise ValueError("per_seconds must be positive")
        self._max = max_events
        self._window = per_seconds
        self._now = now if now is not None else time.monotonic
        self._sleep = sleep
        self._events: deque[float] = deque()

    def _evict(self, t: float) -> None:
        cutoff = t - self._window
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()

    async def acquire(self) -> None:
        """Block until a slot is free, then record this event."""
        while True:
            t = self._now()
            self._evict(t)
            if len(self._events) < self._max:
                self._events.append(t)
                return
            wait = self._events[0] + self._window - t
            await self._sleep(max(wait, 0.0))
