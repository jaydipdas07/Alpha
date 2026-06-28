"""Offline gated re-arm CLI — ``python -m worker.rearm_main [env]`` (default ``paper``).

Clears a LATCHED kill-switch halt the safe way: re-arm ONLY on a clean reconcile
(broker = truth, ADR 0006), for when the pod ``clear_halt`` command channel isn't up
(e.g. a paper worker stuck on a transient feed-stale halt). Builds the worker from
config, runs ``Worker.rearm``, prints the outcome, and exits 0 on re-arm / 1 if refused
— a dirty book leaves the halt latched (never cleared blind). It places no orders and
flips no live gate; it only re-arms what the broker confirms is flat/in-sync.
"""

from __future__ import annotations

import asyncio
import sys

from worker.config import load_dotenv, load_env_config
from worker.loop import build_worker


async def run_rearm(env_name: str = "paper") -> bool:  # pragma: no cover - binds the real venue
    """Build the worker for ``env_name`` and run the gated re-arm; return whether it re-armed."""
    rearmed, detail = await build_worker(load_env_config(env_name)).rearm()
    print(detail)
    return rearmed


def main() -> None:  # pragma: no cover - thin CLI wrapper
    load_dotenv()
    env_name = sys.argv[1] if len(sys.argv) > 1 else "paper"
    raise SystemExit(0 if asyncio.run(run_rearm(env_name)) else 1)


if __name__ == "__main__":  # pragma: no cover
    main()
