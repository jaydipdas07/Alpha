"""Deadman entrypoint — ``python -m worker.deadman_main [env]`` (default ``paper``).

The INDEPENDENT watchdog process (its own systemd unit, its own broker adapter):
it watches the worker's heartbeat file and, if the worker dies beyond the RTO,
flattens the book via broker REST (TEST-5, R1). It shares nothing with the worker
process — that independence is the point.
"""

from __future__ import annotations

import asyncio
import sys

from alpha_core.execution.deadman import Deadman, HeartbeatFile, load_deadman_config
from worker.adapters import build_adapter
from worker.config import active_venue, load_dotenv, load_env_config, load_venues


async def run_deadman(env_name: str = "paper") -> None:
    env = load_env_config(env_name)
    venue_cfg = active_venue(env, load_venues())  # live-gate check
    adapter = build_adapter(venue_cfg, env)  # the deadman's OWN adapter
    deadman = Deadman(
        adapter=adapter,
        liveness=HeartbeatFile(env.heartbeat_path),
        config=load_deadman_config(),
        venue=venue_cfg.venue,
    )
    try:
        await deadman.run()
    finally:
        await adapter.aclose()


def main() -> None:
    load_dotenv()
    env_name = sys.argv[1] if len(sys.argv) > 1 else "paper"
    asyncio.run(run_deadman(env_name))


if __name__ == "__main__":
    main()
