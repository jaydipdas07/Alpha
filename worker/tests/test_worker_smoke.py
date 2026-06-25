"""Smoke test: the worker imports and can see alpha-core (the parity wiring)."""

import alpha_core
import worker


def test_worker_imports_alpha_core() -> None:
    # The worker and the backtester share one alpha-core import — the parity guarantee.
    assert worker.__version__ == "0.0.0"
    assert alpha_core.__version__ == "0.0.0"
