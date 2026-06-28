"""Worker configuration — the adapter registry + the per-environment infra/gate.

Two config files (CLAUDE.md: tunables in ``config/``, no magic numbers):
- ``config/venues.yaml`` — how to reach each venue (only PAPER/testnet venues are
  defined; a live venue + the gate flip are the human's, never CC's).
- ``config/<env>.yaml`` — the active venue, symbols, mode, and **the live gate**
  (``allow_live``). The worker refuses to run live unless the gate is open, and the
  factory refuses to *build* a live adapter (defence in depth; TEST-8 / never-do).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from alpha_core.core.enums import Venue
from alpha_core.helpers.config import load_yaml


def load_dotenv() -> None:
    """Load ``.env`` key=value pairs into the environment if present (no override).

    Broker keys live only in the gitignored ``.env`` (B3); the entrypoints call this
    so the factory can read ``<key_env>_API_KEY`` / ``_API_SECRET``. Honors
    ``ALPHA_DOTENV`` to point at an alternate file (the deploy may stage it elsewhere)."""
    path = Path(os.environ.get("ALPHA_DOTENV", ".env"))
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class VenueConfig(BaseModel):
    """One venue's adapter wiring (a row of ``venues.yaml``)."""

    model_config = ConfigDict(extra="forbid")
    adapter: Literal["ccxt"]
    exchange: str  # ccxt exchange id (binance / delta)
    venue: Venue  # the core Venue enum the OMS/risk tag orders with
    market_type: Literal["spot", "swap", "future"]
    testnet: bool
    streaming: bool = True  # ccxt.pro ws (Binance) vs REST poll (Delta)
    key_env: str  # .env prefix: <key_env>_API_KEY / _API_SECRET


class EnvConfig(BaseModel):
    """A run environment (``<env>.yaml``) — infra + the live gate."""

    model_config = ConfigDict(extra="forbid")
    env: str
    mode: Literal["paper", "live"]
    allow_live: bool = False  # THE LIVE GATE
    worker_id: str
    venue: str  # active venue — a key into venues.yaml
    strategy: str  # the configured edge (strategy registry name); `idle` = no-trade soak
    symbols: list[str] = Field(min_length=1)
    bar_interval_seconds: int = Field(gt=0)
    state_db: str  # SQLAlchemy URL for the durable order/fill/audit StateStore
    heartbeat_path: str
    command_poll_seconds: float = Field(gt=0)
    reconcile_interval_seconds: float = Field(gt=0)  # periodic reconcile + liveness beat cadence

    @model_validator(mode="after")
    def _live_gate(self) -> Self:
        # The gate is consistent both ways: live needs the gate open; paper must keep
        # it shut. CC never produces a config that opens it (that is a human step).
        if self.mode == "live" and not self.allow_live:
            raise ValueError("mode=live requires allow_live=true (the live gate)")
        if self.mode == "paper" and self.allow_live:
            raise ValueError("paper mode must keep the live gate shut (allow_live=false)")
        return self


def load_venues() -> dict[str, VenueConfig]:
    """Load + validate the venue registry from ``config/venues.yaml``."""
    raw = load_yaml("venues.yaml").get("venues", {})
    return {name: VenueConfig.model_validate(v) for name, v in raw.items()}


def load_env_config(env: str = "paper") -> EnvConfig:
    """Load + validate ``config/<env>.yaml`` (default the paper environment)."""
    return EnvConfig.model_validate(load_yaml(f"{env}.yaml"))


def active_venue(env: EnvConfig, venues: dict[str, VenueConfig]) -> VenueConfig:
    """Resolve the env's active venue, enforcing the live gate covers it."""
    if env.venue not in venues:
        raise ValueError(f"env venue {env.venue!r} not in venues.yaml: {sorted(venues)}")
    vc = venues[env.venue]
    # Same condition the factory's build_adapter uses, so the two never drift.
    if not vc.testnet and not (env.allow_live and env.mode == "live"):
        raise ValueError(
            f"venue {env.venue!r} is live but the live gate is shut (allow_live=false)"
        )
    return vc
