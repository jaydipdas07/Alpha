"""Keyed persistent trial ledger (R4, B1a.5) + the DSR-penalty cell-invariance gate."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from alpha_core.backtest.dsr import deflated_sharpe_ratio, expected_max_sharpe
from alpha_core.core.enums import AssetClass
from alpha_core.research.trial_ledger import CellCount, TrialLedger, cell_key


def test_cell_key_matches_pod_format() -> None:
    assert (
        cell_key(AssetClass.CRYPTO, "ma_crossover", "2020-2023") == "crypto|ma_crossover|2020-2023"
    )
    assert cell_key(AssetClass.INDEX_OPTION, "x", "w") == "index_option|x|w"
    with pytest.raises(ValueError, match="separator"):
        cell_key(AssetClass.EQUITY, "a|b", "w")


def test_increment_and_count() -> None:
    led = TrialLedger()  # in-memory
    assert led.count(AssetClass.CRYPTO, "f", "w") == 0
    assert led.increment(AssetClass.CRYPTO, "f", "w") == 1
    assert led.increment(AssetClass.CRYPTO, "f", "w", by=4) == 5
    assert led.count(AssetClass.CRYPTO, "f", "w") == 5
    with pytest.raises(ValueError, match=">= 1"):
        led.increment(AssetClass.CRYPTO, "f", "w", by=0)
    led.close()


def test_increments_have_no_lost_updates() -> None:
    led = TrialLedger()
    for _ in range(300):
        led.increment(AssetClass.EQUITY, "f", "w")
    assert led.count(AssetClass.EQUITY, "f", "w") == 300
    led.close()


def test_persists_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    with TrialLedger(db) as led:
        led.increment(AssetClass.CRYPTO, "f", "w", by=7)
    with TrialLedger(db) as led:  # fresh connection, same file
        assert led.count(AssetClass.CRYPTO, "f", "w") == 7


def test_concurrent_increments_are_atomic(tmp_path: Path) -> None:
    db = tmp_path / "concurrent.db"
    TrialLedger(db).close()  # create the schema/file up front

    def worker() -> None:
        led = TrialLedger(db)  # each thread its own connection to the same file
        for _ in range(50):
            led.increment(AssetClass.CRYPTO, "f", "w")
        led.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with TrialLedger(db) as led:
        assert led.count(AssetClass.CRYPTO, "f", "w") == 200  # 4 x 50, no lost updates


def test_cells_listing() -> None:
    led = TrialLedger()
    led.increment(AssetClass.CRYPTO, "f1", "w")
    led.increment(AssetClass.EQUITY, "f2", "w", by=3)
    cells = led.cells()
    assert CellCount(AssetClass.CRYPTO, "f1", "w", 1) in cells
    assert CellCount(AssetClass.EQUITY, "f2", "w", 3) in cells
    led.close()


def test_dsr_penalty_is_invariant_to_unrelated_cells() -> None:
    """The Done-when: the DSR multiple-testing penalty for one cell depends only on that
    cell's own trial count, so hammering an unrelated cell never changes it."""
    led = TrialLedger()
    returns = [0.1, 0.2, -0.05, 0.15, 0.0, 0.08, -0.1, 0.2] * 20
    var = 0.25
    cell_a = (AssetClass.CRYPTO, "momentum", "2018-2021")
    cell_b = (AssetClass.EQUITY, "mean_reversion", "2010-2015")

    for _ in range(5):
        led.increment(*cell_a)
    n_a = led.count(*cell_a)
    penalty_before = expected_max_sharpe(n_a, var)
    dsr_before = deflated_sharpe_ratio(returns, n_trials=n_a, trial_sharpe_variance=var)

    led.increment(*cell_b, by=10_000)  # an unrelated cell runs a huge search

    assert led.count(*cell_a) == n_a == 5  # cell A's count is untouched
    assert expected_max_sharpe(led.count(*cell_a), var) == penalty_before  # ... so is its penalty
    assert (
        deflated_sharpe_ratio(returns, n_trials=led.count(*cell_a), trial_sharpe_variance=var)
        == dsr_before
    )
    led.close()
