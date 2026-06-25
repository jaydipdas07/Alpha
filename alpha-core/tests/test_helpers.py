"""Tests for decimal_utils, retry, and ratelimit helpers (ADR 0002/0004)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from alpha_core.core.errors import (
    BrokerRateLimited,
    BrokerTimeout,
    InvalidOrder,
)
from alpha_core.helpers.config import RetryConfig
from alpha_core.helpers.decimal_utils import (
    floor_to_lot,
    is_lot_multiple,
    quantize_to_tick,
    to_decimal,
)
from alpha_core.helpers.ratelimit import RateLimiter
from alpha_core.helpers.retry import retry_async

# --- decimal_utils -------------------------------------------------------------


def test_to_decimal_accepts_decimal_int_str() -> None:
    assert to_decimal(5) == Decimal("5")
    assert to_decimal("5.5") == Decimal("5.5")
    assert to_decimal(Decimal("1.25")) == Decimal("1.25")


def test_to_decimal_rejects_float() -> None:
    with pytest.raises(TypeError):
        to_decimal(5.5)  # type: ignore[arg-type]


def test_quantize_to_tick() -> None:
    assert quantize_to_tick(Decimal("100.03"), Decimal("0.05")) == Decimal("100.05")
    assert quantize_to_tick(Decimal("100.02"), Decimal("0.05")) == Decimal("100.00")
    with pytest.raises(ValueError):
        quantize_to_tick(Decimal("100"), Decimal("0"))


def test_floor_to_lot_and_multiple() -> None:
    assert floor_to_lot(Decimal("57"), Decimal("25")) == Decimal("50")
    assert is_lot_multiple(Decimal("50"), Decimal("25")) is True
    assert is_lot_multiple(Decimal("57"), Decimal("25")) is False


# --- retry ---------------------------------------------------------------------


async def _noop_sleep(_: float) -> None:
    return None


@pytest.mark.asyncio
async def test_retry_succeeds_after_transient() -> None:
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise BrokerTimeout("again")
        return "ok"

    cfg = RetryConfig(max_attempts=5, base_backoff=0.01)
    result = await retry_async(flaky, cfg, sleep=_noop_sleep)
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retry_gives_up_after_max_attempts() -> None:
    calls = {"n": 0}

    async def always_timeout() -> str:
        calls["n"] += 1
        raise BrokerTimeout("nope")

    cfg = RetryConfig(max_attempts=3, base_backoff=0.01)
    with pytest.raises(BrokerTimeout):
        await retry_async(always_timeout, cfg, sleep=_noop_sleep)
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retry_does_not_retry_terminal() -> None:
    calls = {"n": 0}

    async def terminal() -> str:
        calls["n"] += 1
        raise InvalidOrder("bad")

    cfg = RetryConfig(max_attempts=5, base_backoff=0.01)
    with pytest.raises(InvalidOrder):
        await retry_async(terminal, cfg, sleep=_noop_sleep)
    assert calls["n"] == 1  # tried once, never retried


@pytest.mark.asyncio
async def test_retry_honors_rate_limit_retry_after() -> None:
    slept: list[float] = []

    async def record_sleep(d: float) -> None:
        slept.append(d)

    calls = {"n": 0}

    async def limited() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise BrokerRateLimited("429", retry_after=4.0)
        return "ok"

    cfg = RetryConfig(max_attempts=3, base_backoff=0.01, max_backoff=1.0, jitter=False)
    result = await retry_async(limited, cfg, sleep=record_sleep)
    assert result == "ok"
    assert slept and slept[0] >= 4.0  # retry_after dominates the capped backoff


# --- ratelimit -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limiter_allows_within_cap() -> None:
    clock = {"t": 0.0}
    limiter = RateLimiter(3, 60.0, now=lambda: clock["t"], sleep=_noop_sleep)
    for _ in range(3):
        await limiter.acquire()  # no wait for the first 3 in the window


@pytest.mark.asyncio
async def test_rate_limiter_waits_when_full() -> None:
    clock = {"t": 0.0}
    waited: list[float] = []

    async def fake_sleep(d: float) -> None:
        waited.append(d)
        clock["t"] += d  # advance virtual time so the next acquire proceeds

    limiter = RateLimiter(2, 60.0, now=lambda: clock["t"], sleep=fake_sleep)
    await limiter.acquire()  # t=0
    await limiter.acquire()  # t=0
    await limiter.acquire()  # full -> must wait ~60s for the oldest to expire
    assert waited and abs(waited[0] - 60.0) < 1e-9
