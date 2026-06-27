"""Worker config + live-gate tests (M3.1/M3.2)."""

from __future__ import annotations

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
        "symbols": ["BTC/USDT"],
        "bar_interval_seconds": 60,
        "heartbeat_path": "var/run/hb",
        "command_poll_seconds": 1.0,
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


def test_shipped_venues_are_all_testnet() -> None:
    venues = load_venues()
    assert set(venues) >= {"binance-spot-testnet", "delta-testnet"}
    assert all(v.testnet for v in venues.values())  # no live venue is defined (human's)


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
    assert vc.testnet is True and vc.venue is Venue.BINANCE
