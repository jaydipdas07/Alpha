"""Worker config + live-gate tests (M3.1/M3.2)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from alpha_core.core.enums import Venue
from worker.config import EnvConfig, VenueConfig, active_venue, load_env_config, load_venues


def _env(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "env": "paper",
        "mode": "paper",
        "allow_live": False,
        "worker_id": "w1",
        "venue": "binance-spot-testnet",
        "strategy": "idle",
        "symbols": ["BTC/USDT"],
        "bar_interval_seconds": 60,
        "state_db": "sqlite:///:memory:",
        "heartbeat_path": "var/run/hb",
        "command_poll_seconds": 1.0,
        "reconcile_interval_seconds": 30,
    }
    base.update(over)
    return base


def _venue(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "adapter": "ccxt",
        "exchange": "binance",
        "venue": "BINANCE",
        "market_type": "spot",
        "testnet": True,
        "streaming": True,
        "key_env": "BINANCE_TESTNET",
    }
    base.update(over)
    return base


# --- the shipped config loads + is paper-safe ----------------------------------


def test_shipped_venues_are_all_testnet_or_data_only() -> None:
    # No venue that can take LIVE ORDERS is defined (that config is the human's).
    # ccxt venues carry an order surface -> must be testnet; kite is market-data
    # only by construction (no order adapter exists) and paper-only by validation.
    venues = load_venues()
    assert set(venues) >= {"binance-spot-testnet", "delta-testnet", "kite-nse"}
    assert all(v.testnet for v in venues.values() if v.adapter == "ccxt")


def test_shipped_paper_env_keeps_the_gate_shut() -> None:
    env = load_env_config("paper")
    assert env.mode == "paper" and env.allow_live is False
    assert env.venue in load_venues()


# --- the live gate -------------------------------------------------------------


def test_live_mode_requires_the_gate_open() -> None:
    with pytest.raises(ValidationError, match="live gate"):
        EnvConfig.model_validate(_env(mode="live", allow_live=False))


def test_paper_mode_must_keep_the_gate_shut() -> None:
    with pytest.raises(ValidationError, match="live gate shut"):
        EnvConfig.model_validate(_env(mode="paper", allow_live=True))


def test_live_with_gate_open_is_accepted() -> None:
    env = EnvConfig.model_validate(_env(mode="live", allow_live=True))
    assert env.mode == "live" and env.allow_live is True


def test_active_venue_refuses_live_venue_without_the_gate() -> None:
    venues = {"x": VenueConfig.model_validate(_venue(testnet=False))}
    env = EnvConfig.model_validate(_env(venue="x"))  # paper, gate shut
    with pytest.raises(ValueError, match="live gate is shut"):
        active_venue(env, venues)


def test_active_venue_unknown_venue_errors() -> None:
    env = EnvConfig.model_validate(_env(venue="nope"))
    with pytest.raises(ValueError, match="not in venues"):
        active_venue(env, {})


def test_active_venue_resolves_testnet_venue() -> None:
    venues = load_venues()
    env = load_env_config("paper")
    vc = active_venue(env, venues)
    assert vc.testnet is True
    assert vc.venue in (Venue.BINANCE, Venue.DELTA)  # whichever the shipped paper venue is


def test_pod_sync_deployment_id_must_be_a_uuid() -> None:
    # The id is interpolated into the trade sync's baseline SELECTs — a malformed value
    # must die at config load, never reach a query.
    base = {
        "env": "paper",
        "mode": "paper",
        "allow_live": False,
        "worker_id": "w",
        "venue": "delta-testnet",
        "strategy": "idle",
        "symbols": ["BTC/USD:USD"],
        "bar_interval_seconds": 60,
        "state_db": "sqlite:///:memory:",
        "heartbeat_path": "/tmp/hb",
        "command_poll_seconds": 1.0,
        "reconcile_interval_seconds": 30,
    }
    good = {**base, "pod_sync": {"pod_id": "p", "deployment_id": str(uuid.uuid4())}}
    assert EnvConfig.model_validate(good).pod_sync is not None
    with pytest.raises(ValidationError):
        EnvConfig.model_validate(
            {**base, "pod_sync": {"pod_id": "p", "deployment_id": "1; DROP TABLE orders"}}
        )


def _m45_base() -> dict[str, object]:
    return {
        "env": "kite-paper",
        "mode": "paper",
        "allow_live": False,
        "worker_id": "w",
        "venue": "kite-nse",
        "strategy": "idle",
        "symbols": ["NSE:RELIANCE"],
        "bar_interval_seconds": 60,
        "state_db": "sqlite:///:memory:",
        "heartbeat_path": "/tmp/hb",
        "command_poll_seconds": 1.0,
        "reconcile_interval_seconds": 30,
    }


def test_kite_venue_is_paper_only_and_bypasses_the_live_gate() -> None:
    # Data-only kite (no order surface) may run without the live gate — but ONLY
    # under paper execution; venue execution against kite must fail fast.
    venues = load_venues()
    assert "kite-nse" in venues and venues["kite-nse"].adapter == "kite"
    paper_env = EnvConfig.model_validate({**_m45_base(), "execution": "paper"})
    assert active_venue(paper_env, venues).venue is Venue.NSE  # allowed, gate untouched
    venue_env = EnvConfig.model_validate(_m45_base())  # execution defaults to "venue"
    with pytest.raises(ValueError, match="data-only"):
        active_venue(venue_env, venues)


def test_live_mode_contradicts_paper_execution() -> None:
    with pytest.raises(ValidationError, match="paper never goes live"):
        EnvConfig.model_validate(
            {**_m45_base(), "mode": "live", "allow_live": True, "execution": "paper"}
        )


def test_paper_starting_cash_is_decimal_never_float() -> None:
    env = EnvConfig.model_validate({**_m45_base(), "paper_starting_cash": "250000.50"})
    from decimal import Decimal

    assert env.paper_starting_cash == Decimal("250000.50")
    assert isinstance(env.paper_starting_cash, Decimal)
