"""Persistent proposal-fingerprint ledger (B1b.3 precondition) — idempotent, durable, cell-keyed."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from alpha_core.backtest.dsr import deflated_sharpe_ratio, expected_max_sharpe
from alpha_core.core.enums import AssetClass
from alpha_core.research.proposal_ledger import ProposalLedger, RecordResult, cell_key

_CRYPTO = AssetClass.CRYPTO


def test_cell_key_matches_pod_format() -> None:
    assert cell_key(_CRYPTO, "ma_crossover", "2020-2023") == "crypto|ma_crossover|2020-2023"
    assert cell_key(AssetClass.INDEX_OPTION, "x", "w") == "index_option|x|w"
    with pytest.raises(ValueError, match="separator"):
        cell_key(AssetClass.EQUITY, "a|b", "w")
    with pytest.raises(ValueError, match="64 chars"):
        cell_key(AssetClass.EQUITY, "f" * 65, "w")  # exceeds the pod column limit


def test_dsr_penalty_is_invariant_to_unrelated_cells() -> None:
    # 1a.GATE condition (R4, migrated from the retired TrialLedger): the DSR multiple-testing
    # penalty for one cell depends only on that cell's own trial count, so hammering an unrelated
    # cell never changes it.
    returns = [0.1, 0.2, -0.05, 0.15, 0.0, 0.08, -0.1, 0.2] * 20
    var = 0.25
    cell_a = (_CRYPTO, "momentum", "2018-2021")
    cell_b = (AssetClass.EQUITY, "mean_reversion", "2010-2015")
    with ProposalLedger() as led:
        for i in range(5):
            led.record(*cell_a, f"a{i}")
        n_a = led.count(*cell_a)
        penalty_before = expected_max_sharpe(n_a, var)
        dsr_before = deflated_sharpe_ratio(returns, n_trials=n_a, trial_sharpe_variance=var)

        for i in range(100):  # an unrelated cell runs a large search
            led.record(*cell_b, f"b{i}")

        # cell A's count is untouched by the unrelated cell B, so its DSR penalty is too.
        assert led.count(*cell_a) == n_a == 5
        assert expected_max_sharpe(led.count(*cell_a), var) == penalty_before
        assert (
            deflated_sharpe_ratio(returns, n_trials=led.count(*cell_a), trial_sharpe_variance=var)
            == dsr_before
        )


def test_recording_is_idempotent_so_the_count_cannot_be_inflated() -> None:
    # the whole point: re-recording the same proposal is a no-op -> the trial count the DSR
    # deflates by can never be double-counted by a loop that re-proposes a config.
    with ProposalLedger() as ledger:
        first = ledger.record(_CRYPTO, "rsi_bollinger", "2020-2024", "abc123")
        assert first == RecordResult(is_new=True, count=1)
        second = ledger.record(_CRYPTO, "rsi_bollinger", "2020-2024", "abc123")
        assert second.is_new is False
        assert second.count == 1  # unchanged — not 2
        assert ledger.count(_CRYPTO, "rsi_bollinger", "2020-2024") == 1


def test_distinct_fingerprints_grow_the_count() -> None:
    with ProposalLedger() as ledger:
        for i, fp in enumerate(("a", "b", "c"), start=1):
            result = ledger.record(_CRYPTO, "ma_crossover", "w", fp)
            assert result.is_new is True
            assert result.count == i
        assert ledger.seen(_CRYPTO, "ma_crossover", "w") == {"a", "b", "c"}


def test_count_is_cell_invariant() -> None:
    # recording in one cell never changes another cell's count (the keyed property the DSR relies
    # on, R4) — mirrors the B1a.5 trial-ledger invariance.
    with ProposalLedger() as ledger:
        for fp in ("a", "b", "c", "d", "e"):
            ledger.record(AssetClass.EQUITY, "momentum_roc", "2021", fp)
        ledger.record(_CRYPTO, "ma_crossover", "2021", "z")
        assert ledger.count(_CRYPTO, "ma_crossover", "2021") == 1  # untouched by the equity cell
        assert ledger.count(AssetClass.EQUITY, "momentum_roc", "2021") == 5


def test_originality_persists_across_instances(tmp_path: Path) -> None:
    # cross-run originality: a fresh ledger on the same file sees what was tried before (so the
    # strategist's `seen` survives a restart, which is exactly why this store must exist).
    db = tmp_path / "proposals.db"
    with ProposalLedger(db) as ledger:
        ledger.record(_CRYPTO, "vwap_reversion", "w", "fp1")
        ledger.record(_CRYPTO, "vwap_reversion", "w", "fp2")
    with ProposalLedger(db) as reopened:
        assert reopened.seen(_CRYPTO, "vwap_reversion", "w") == {"fp1", "fp2"}
        assert reopened.count(_CRYPTO, "vwap_reversion", "w") == 2
        assert reopened.record(_CRYPTO, "vwap_reversion", "w", "fp1").is_new is False  # still seen


def test_same_fingerprint_in_two_cells_is_counted_once_each() -> None:
    # the composite PK (cell_key, fingerprint): the same fingerprint can live in two cells, counted
    # once in each -> proves cell_key is genuinely part of the key, not just the fingerprint.
    with ProposalLedger() as ledger:
        assert ledger.record(_CRYPTO, "ma_crossover", "w", "x").is_new is True
        assert ledger.record(AssetClass.EQUITY, "ma_crossover", "w", "x").is_new is True
        assert ledger.count(_CRYPTO, "ma_crossover", "w") == 1
        assert ledger.count(AssetClass.EQUITY, "ma_crossover", "w") == 1


def test_concurrent_records_are_atomic(tmp_path: Path) -> None:
    # the "safe across processes" guarantee: 8 threads (each its own connection on one WAL file)
    # race to record the SAME 50 fingerprints; is_new must fire exactly once per fingerprint and
    # the final count must be exactly the distinct set -- no double-counting under contention.
    db = tmp_path / "proposals.db"
    fingerprints = [f"fp{i}" for i in range(50)]
    new_count = 0
    guard = threading.Lock()

    def worker() -> None:
        nonlocal new_count
        with ProposalLedger(db) as ledger:
            for fp in fingerprints:
                if ledger.record(_CRYPTO, "ma_crossover", "w", fp).is_new:
                    with guard:
                        new_count += 1

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with ProposalLedger(db) as ledger:
        assert ledger.count(_CRYPTO, "ma_crossover", "w") == 50  # exactly the distinct set
    assert new_count == 50  # is_new fired once per fingerprint despite 8x contention


def test_unseen_cell_is_empty() -> None:
    with ProposalLedger() as ledger:
        assert ledger.count(_CRYPTO, "ma_crossover", "never") == 0
        assert ledger.seen(_CRYPTO, "ma_crossover", "never") == set()


def test_cells_lists_every_cell_with_its_count() -> None:
    with ProposalLedger() as ledger:
        ledger.record(_CRYPTO, "ma_crossover", "w", "a")
        ledger.record(_CRYPTO, "ma_crossover", "w", "b")
        ledger.record(AssetClass.EQUITY, "rsi_bollinger", "w", "c")
        cells = ledger.cells()
        assert {(c.market, c.family, c.cumulative_trials) for c in cells} == {
            (_CRYPTO, "ma_crossover", 2),
            (AssetClass.EQUITY, "rsi_bollinger", 1),
        }


def test_bad_cell_key_is_rejected() -> None:
    with ProposalLedger() as ledger, pytest.raises(ValueError, match="must not contain the"):
        ledger.record(_CRYPTO, "fam", "2020|2024", "fp")  # '|' is the key separator
