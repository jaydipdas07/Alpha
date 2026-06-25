"""Research heartbeat staleness + work-lease logic (R8): a dead box never hangs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alpha_core.research.lease import Lease, ResearchConfig, acquire, is_stale, renew

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_TTL = timedelta(seconds=1800)


def test_is_stale() -> None:
    timeout = timedelta(seconds=120)
    assert not is_stale(_T0, _T0 + timedelta(seconds=60), timeout)  # fresh
    assert not is_stale(_T0, _T0 + timedelta(seconds=120), timeout)  # exactly at -> not yet
    assert is_stale(_T0, _T0 + timedelta(seconds=121), timeout)  # stale


def test_is_stale_requires_utc() -> None:
    with pytest.raises(ValueError, match="tz-aware UTC"):
        is_stale(datetime(2026, 1, 1), _T0, timedelta(seconds=60))  # naive last_seen


def test_lease_requires_utc() -> None:
    with pytest.raises(ValueError, match="tz-aware UTC"):
        Lease(holder="r1", acquired_at=datetime(2026, 1, 1), expires_at=_T0 + _TTL)  # naive


def test_lease_is_expired() -> None:
    lease = Lease(holder="r1", acquired_at=_T0, expires_at=_T0 + _TTL)
    assert not lease.is_expired(_T0 + timedelta(seconds=1799))
    assert lease.is_expired(_T0 + _TTL)  # exactly at expiry -> reclaimable
    assert lease.is_expired(_T0 + timedelta(seconds=3600))


def test_acquire_free() -> None:
    lease = acquire(None, "r1", _T0, _TTL)
    assert lease is not None
    assert lease.holder == "r1"
    assert lease.expires_at == _T0 + _TTL


def test_acquire_blocked_by_live_other_holder() -> None:
    held = acquire(None, "r1", _T0, _TTL)
    assert held is not None
    # r2 cannot steal r1's still-live lease
    assert acquire(held, "r2", _T0 + timedelta(seconds=10), _TTL) is None


def test_expired_lease_is_reclaimable_never_hangs() -> None:
    # r1 claims the work then dies; past expiry r2 reclaims it -> no permanent hang (R8)
    held = acquire(None, "r1", _T0, _TTL)
    assert held is not None
    later = _T0 + _TTL + timedelta(seconds=1)
    reclaimed = acquire(held, "r2", later, _TTL)
    assert reclaimed is not None
    assert reclaimed.holder == "r2"


def test_same_holder_reacquires() -> None:
    held = acquire(None, "r1", _T0, _TTL)
    assert held is not None
    again = acquire(held, "r1", _T0 + timedelta(seconds=5), _TTL)
    assert again is not None
    assert again.holder == "r1"


def test_renew_extends_expiry() -> None:
    held = acquire(None, "r1", _T0, _TTL)
    assert held is not None
    renewed = renew(held, _T0 + timedelta(seconds=600), _TTL)
    assert renewed.expires_at == _T0 + timedelta(seconds=600) + _TTL
    assert renewed.holder == "r1"
    assert renewed.acquired_at == _T0  # acquired_at preserved


def test_config_defaults_and_timedeltas() -> None:
    cfg = ResearchConfig()
    assert cfg.heartbeat_timeout == timedelta(seconds=120)
    assert cfg.lease_ttl == timedelta(seconds=1800)


def test_config_from_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPHA_CONFIG_DIR", raising=False)  # use the repo-root config/
    cfg = ResearchConfig.from_config()
    assert cfg.heartbeat_timeout_seconds == 120
    assert cfg.lease_ttl_seconds == 1800
