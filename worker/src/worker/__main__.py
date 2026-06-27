"""Worker entrypoint — ``python -m worker [env]`` (default ``paper``).

Loads ``.env`` (broker keys, B3) into the environment, then runs the loop. The
live gate stays in config: this entrypoint refuses nothing on its own — the
factory + EnvConfig enforce paper/testnet (CLAUDE.md never-do).
"""

from __future__ import annotations

import asyncio
import sys

from worker.config import load_dotenv
from worker.loop import run_worker


def main() -> None:
    load_dotenv()
    env_name = sys.argv[1] if len(sys.argv) > 1 else "paper"
    asyncio.run(run_worker(env_name))


if __name__ == "__main__":
    main()
