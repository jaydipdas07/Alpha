"""The constrained strategist (B1b.1b) — proposes valid, original configs; holdout untouched."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import pytest

from alpha_core.core.enums import AssetClass
from alpha_core.core.interfaces import Strategy
from alpha_core.research import strategist as strategist_module
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.strategist import (
    TEMPLATES,
    CellSaturated,
    DecimalRange,
    IntRange,
    ParamSpace,
    ParamValue,
    RandomProposer,
    Strategist,
    StrategistError,
    StrategyProposal,
    proposal_fingerprint,
)

_CRYPTO = AssetClass.CRYPTO


class _ScriptedProposer:
    """Yields a fixed sequence of param dicts (to drive the validate-by-build path in tests)."""

    def __init__(self, scripts: list[dict[str, ParamValue]]) -> None:
        self._scripts = iter(scripts)

    def propose(self, space: ParamSpace) -> dict[str, ParamValue]:
        return dict(next(self._scripts))


class _ConstantProposer:
    """Always proposes the same params (to drive the exhausted-space path)."""

    def __init__(self, params: dict[str, ParamValue]) -> None:
        self._params = params

    def propose(self, space: ParamSpace) -> dict[str, ParamValue]:
        return dict(self._params)


# --- the Done-when: proposes a valid (buildable) config, holdout untouched ---------------------


def test_proposes_a_valid_buildable_config_for_every_template() -> None:
    with ProposalLedger() as ledger:
        strategist = Strategist(ledger, proposer=RandomProposer(seed=7))
        for name, template in TEMPLATES.items():
            proposal = strategist.propose(name, market=_CRYPTO, window="2020-2024")
            assert isinstance(proposal, StrategyProposal)
            assert proposal.template == name
            # the proposed params build a real, valid strategy (the Done-when).
            assert isinstance(template.build(proposal.params), Strategy)
            assert proposal.fingerprint in ledger.seen(_CRYPTO, name, "2020-2024")
            assert proposal.trial_index == 1  # first trial in this (fresh) cell


def test_strategist_takes_no_data_so_the_holdout_is_unreachable() -> None:
    # TEST-3 is STRUCTURAL: propose() has no data/bars/holdout parameter (only cell metadata), and
    # the module imports no data/holdout reader — the strategist simply has no path to the holdout.
    sig_params = set(inspect.signature(Strategist.propose).parameters) - {"self"}
    assert sig_params == {"template_name", "market", "window"}
    src = inspect.getsource(strategist_module)
    assert "alpha_core.data" not in src  # no cold store / holdout reader (either import form)
    assert "HoldoutStore" not in src and "read_bars" not in src


# --- originality + trial accounting (now the durable, idempotent ledger) ------------------------


def test_originality_never_reproposes_a_recorded_config() -> None:
    with ProposalLedger() as ledger:
        strategist = Strategist(ledger, proposer=RandomProposer(seed=1))
        fingerprints = [
            strategist.propose("momentum_roc", market=_CRYPTO, window="w").fingerprint
            for _ in range(10)
        ]
        assert len(fingerprints) == len(set(fingerprints))  # all distinct — no repeats
        assert ledger.seen(_CRYPTO, "momentum_roc", "w") == set(fingerprints)


def test_ledger_counts_once_per_accepted_proposal() -> None:
    with ProposalLedger() as ledger:
        strategist = Strategist(ledger, proposer=RandomProposer(seed=3))
        for _ in range(5):
            strategist.propose("ma_crossover", market=AssetClass.EQUITY, window="2021")
        assert ledger.count(AssetClass.EQUITY, "ma_crossover", "2021") == 5


def test_originality_persists_across_strategist_runs(tmp_path: Path) -> None:
    # the desync fix end-to-end: a second run on the same ledger file never re-proposes a config
    # from the first (even reusing a seed), and never re-counts one — the durable, idempotent
    # ledger is the only originality + trial-count source.
    db = tmp_path / "proposals.db"
    with ProposalLedger(db) as ledger:
        first = {
            Strategist(ledger, proposer=RandomProposer(seed=s))
            .propose("vwap_reversion", market=_CRYPTO, window="w")
            .fingerprint
            for s in range(6)
        }
    with ProposalLedger(db) as ledger:
        strategist = Strategist(ledger, proposer=RandomProposer(seed=0))  # a reused seed
        more = {
            strategist.propose("vwap_reversion", market=_CRYPTO, window="w").fingerprint
            for _ in range(3)
        }
        assert first.isdisjoint(more)  # nothing from run 1 is re-proposed in run 2
        assert ledger.count(_CRYPTO, "vwap_reversion", "w") == len(first) + len(more)


def test_invalid_params_are_resampled_until_valid() -> None:
    # the proposer first yields an INVALID ma_crossover (fast >= slow -> __init__ raises), then a
    # valid one; the strategist resamples and only the valid proposal is recorded.
    proposer = _ScriptedProposer(
        [
            {"fast_period": 30, "slow_period": 10},  # invalid: require 0 < fast < slow
            {"fast_period": 5, "slow_period": 20},  # valid
        ]
    )
    with ProposalLedger() as ledger:
        strategist = Strategist(ledger, proposer=proposer)
        proposal = strategist.propose("ma_crossover", market=_CRYPTO, window="w")
        assert proposal.params == {"fast_period": 5, "slow_period": 20}
        assert ledger.count(_CRYPTO, "ma_crossover", "w") == 1  # the invalid one was not recorded


# --- determinism + error paths -----------------------------------------------------------------


def test_proposal_is_deterministic_under_seed() -> None:
    def run() -> StrategyProposal:
        with ProposalLedger() as ledger:
            return Strategist(ledger, proposer=RandomProposer(seed=42)).propose(
                "rsi_bollinger", market=_CRYPTO, window="w"
            )

    first, second = run(), run()
    assert first.params == second.params
    assert first.fingerprint == second.fingerprint


def test_unknown_template_raises() -> None:
    with ProposalLedger() as ledger, pytest.raises(StrategistError, match="unknown template"):
        Strategist(ledger).propose("no_such_template", market=_CRYPTO, window="w")


def test_exhausted_space_raises_when_every_proposal_is_already_recorded() -> None:
    # a proposer that always yields the same (valid) config whose fingerprint is already recorded
    # -> no original proposal exists -> StrategistError, and the count is not inflated.
    params: dict[str, ParamValue] = {"band_bps": Decimal("50")}
    with ProposalLedger() as ledger:
        ledger.record(
            _CRYPTO, "vwap_reversion", "w", proposal_fingerprint("vwap_reversion", params)
        )
        strategist = Strategist(ledger, proposer=_ConstantProposer(params), max_attempts=5)
        with pytest.raises(CellSaturated, match="the cell is saturated"):
            strategist.propose("vwap_reversion", market=_CRYPTO, window="w")
        assert ledger.count(_CRYPTO, "vwap_reversion", "w") == 1  # unchanged — idempotent no-ops


def test_invalid_cell_raises_strategist_error() -> None:
    # a window the ledger's cell_key rejects (contains the '|' delimiter) surfaces as a
    # StrategistError (the documented contract), not a bare ValueError.
    with ProposalLedger() as ledger, pytest.raises(StrategistError, match="invalid cell"):
        Strategist(ledger).propose("ma_crossover", market=_CRYPTO, window="2020|2024")


# --- the param space + fingerprint -------------------------------------------------------------


def test_param_spec_rejects_bad_ranges() -> None:
    with pytest.raises(ValueError, match="low 10 > high 3"):
        IntRange(10, 3)
    with pytest.raises(ValueError, match="must divide"):
        DecimalRange(Decimal("0"), Decimal("1"), Decimal("0.3"))
    with pytest.raises(ValueError, match="bad DecimalRange"):
        DecimalRange(Decimal("5"), Decimal("1"), Decimal("1"))


def test_fingerprint_is_stable_order_independent_and_value_sensitive() -> None:
    base: Mapping[str, ParamValue] = {"a": 1, "b": Decimal("2.0")}
    reordered: Mapping[str, ParamValue] = {"b": Decimal("2.0"), "a": 1}
    assert proposal_fingerprint("x", base) == proposal_fingerprint("x", reordered)  # order-free
    # Decimal is canonicalized: 2.0 and 2 are the same logical value (a future LLM proposer that
    # emits Decimal('2') must not fork originality from RandomProposer's Decimal('2.0')).
    assert proposal_fingerprint("x", base) == proposal_fingerprint("x", {"a": 1, "b": Decimal("2")})
    assert proposal_fingerprint("x", base) != proposal_fingerprint(
        "x", {"a": 1, "b": Decimal("2.5")}
    )
    assert proposal_fingerprint("x", base) != proposal_fingerprint("y", base)  # template-sensitive
