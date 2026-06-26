"""Persistent proposal-fingerprint ledger (B1b.3 precondition) — idempotent, durable, cell-keyed."""

from __future__ import annotations

from pathlib import Path

import pytest

from alpha_core.core.enums import AssetClass
from alpha_core.research.proposal_ledger import ProposalLedger

_CRYPTO = AssetClass.CRYPTO


def test_recording_is_idempotent_so_the_count_cannot_be_inflated() -> None:
    # the whole point: re-recording the same proposal is a no-op -> the trial count the DSR
    # deflates by can never be double-counted by a loop that re-proposes a config.
    with ProposalLedger() as ledger:
        first = ledger.record(_CRYPTO, "rsi_bollinger", "2020-2024", "abc123")
        assert first == type(first)(is_new=True, count=1)
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
