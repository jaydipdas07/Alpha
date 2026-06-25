"""Heartbeat / watchdog (ADR 0012 RB-27).

The bot beats periodically; an external watchdog checks ``is_alive`` and alerts
if the bot stops emitting beyond ``interval * miss_factor``. The clock is
injectable so tests are deterministic.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class Heartbeat:
    """Tracks the last beat and reports liveness against a staleness budget."""

    def __init__(
        self,
        interval_seconds: float,
        *,
        miss_factor: float = 3.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if miss_factor < 1:
            raise ValueError("miss_factor must be >= 1")
        self._interval = interval_seconds
        self._miss_factor = miss_factor
        self._now = now if now is not None else time.monotonic
        self._last_beat: float | None = None

    def beat(self) -> None:
        """Record a heartbeat now."""
        self._last_beat = self._now()

    @property
    def last_beat(self) -> float | None:
        return self._last_beat

    def is_alive(self) -> bool:
        """True iff a beat occurred within ``interval * miss_factor``."""
        if self._last_beat is None:
            return False
        return (self._now() - self._last_beat) <= self._interval * self._miss_factor
