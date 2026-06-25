"""Retry policy for idempotent broker calls (ADR 0004).

Retries ONLY ``TransientBrokerError`` (timeout/5xx/429/conn-lost), never a
terminal error, with capped exponential backoff + jitter. ``BrokerRateLimited``
honors the venue's ``retry_after`` when present. Only idempotent operations may
be wrapped (the caller guarantees this).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from alpha_core.core.errors import BrokerRateLimited, TransientBrokerError
from alpha_core.helpers.config import RetryConfig


def _backoff_delay(attempt: int, cfg: RetryConfig) -> float:
    """Exponential backoff for ``attempt`` (1-based), capped, with optional jitter."""
    raw = cfg.base_backoff * (2 ** (attempt - 1))
    delay = min(raw, cfg.max_backoff)
    if cfg.jitter:
        delay = random.uniform(0, delay)
    return float(delay)


async def retry_async[T](
    func: Callable[[], Awaitable[T]],
    cfg: RetryConfig,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Call ``func`` with retries on ``TransientBrokerError`` only.

    ``sleep`` is injectable so tests run without real delays. After
    ``cfg.max_attempts`` the last transient error propagates (the caller escalates
    to reconcile + kill switch). Terminal errors propagate immediately.
    """
    if cfg.max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    last_exc: TransientBrokerError | None = None
    for attempt in range(1, cfg.max_attempts + 1):
        try:
            return await func()
        except TransientBrokerError as exc:
            last_exc = exc
            if attempt == cfg.max_attempts:
                break
            delay = _backoff_delay(attempt, cfg)
            if isinstance(exc, BrokerRateLimited) and exc.retry_after is not None:
                delay = max(delay, exc.retry_after)
            await sleep(delay)
    assert last_exc is not None
    raise last_exc
