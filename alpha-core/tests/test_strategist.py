"""The constrained strategist (B1b.1b) — proposes valid, original configs; holdout untouched."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from decimal import Decimal

import pytest

from alpha_core.core.enums import AssetClass
from alpha_core.core.interfaces import Strategy
from alpha_core.research import strategist as strategist_module
from alpha_core.research.strategist import (
    TEMPLATES,
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
from alpha_core.research.trial_ledger import TrialLedger


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
    with TrialLedger() as ledger:
        strategist = Strategist(ledger, proposer=RandomProposer(seed=7))
        for name, template in TEMPLATES.items():
            seen: set[str] = set()
            proposal = strategist.propose(
                name, market=AssetClass.CRYPTO, window="2020-2024", seen=seen
            )
            assert isinstance(proposal, StrategyProposal)
            assert proposal.template == name
            # the proposed params build a real, valid strategy (the Done-when).
            assert isinstance(template.build(proposal.params), Strategy)
            assert proposal.fingerprint in seen
            assert proposal.trial_index == 1  # first trial in this (fresh) cell


def test_strategist_takes_no_data_so_the_holdout_is_unreachable() -> None:
    # TEST-3 is STRUCTURAL here: propose() has no data/bars/holdout parameter (only cell metadata
    # + the ledger + seen), and the module imports no data/holdout reader — the strategist simply
    # has no path to the holdout.
    sig_params = set(inspect.signature(Strategist.propose).parameters) - {"self"}
    assert sig_params == {"template_name", "market", "window", "seen"}
    src = inspect.getsource(strategist_module)
    assert "from alpha_core.data" not in src  # no cold store / holdout reader imported
    assert "HoldoutStore" not in src and "read_bars" not in src


# --- originality + trial accounting ------------------------------------------------------------


def test_originality_never_reproposes_a_seen_config() -> None:
    with TrialLedger() as ledger:
        strategist = Strategist(ledger, proposer=RandomProposer(seed=1))
        seen: set[str] = set()
        fingerprints = [
            strategist.propose(
                "momentum_roc", market=AssetClass.CRYPTO, window="w", seen=seen
            ).fingerprint
            for _ in range(10)
        ]
        assert len(fingerprints) == len(set(fingerprints))  # all distinct — no repeats
        assert seen == set(fingerprints)


def test_ledger_increments_once_per_accepted_proposal() -> None:
    with TrialLedger() as ledger:
        strategist = Strategist(ledger, proposer=RandomProposer(seed=3))
        seen: set[str] = set()
        for _ in range(5):
            strategist.propose("ma_crossover", market=AssetClass.EQUITY, window="2021", seen=seen)
        assert ledger.count(AssetClass.EQUITY, "ma_crossover", "2021") == 5


def test_invalid_params_are_resampled_until_valid() -> None:
    # the proposer first yields an INVALID ma_crossover (fast >= slow -> __init__ raises), then a
    # valid one; the strategist resamples and only the valid proposal counts.
    proposer = _ScriptedProposer(
        [
            {"fast_period": 30, "slow_period": 10},  # invalid: require 0 < fast < slow
            {"fast_period": 5, "slow_period": 20},  # valid
        ]
    )
    with TrialLedger() as ledger:
        strategist = Strategist(ledger, proposer=proposer)
        proposal = strategist.propose(
            "ma_crossover", market=AssetClass.CRYPTO, window="w", seen=set()
        )
        assert proposal.params == {"fast_period": 5, "slow_period": 20}
        assert ledger.count(AssetClass.CRYPTO, "ma_crossover", "w") == 1  # invalid one not counted


# --- determinism + error paths -----------------------------------------------------------------


def test_proposal_is_deterministic_under_seed() -> None:
    def run() -> StrategyProposal:
        with TrialLedger() as ledger:
            strategist = Strategist(ledger, proposer=RandomProposer(seed=42))
            return strategist.propose(
                "rsi_bollinger", market=AssetClass.CRYPTO, window="w", seen=set()
            )

    first, second = run(), run()
    assert first.params == second.params
    assert first.fingerprint == second.fingerprint


def test_unknown_template_raises() -> None:
    with TrialLedger() as ledger, pytest.raises(StrategistError, match="unknown template"):
        Strategist(ledger).propose(
            "no_such_template", market=AssetClass.CRYPTO, window="w", seen=set()
        )


def test_exhausted_space_raises_when_every_proposal_is_seen() -> None:
    # a proposer that always yields the same (valid) config whose fingerprint is already seen ->
    # no original proposal exists -> StrategistError.
    params: dict[str, ParamValue] = {"band_bps": Decimal("50")}
    with TrialLedger() as ledger:
        strategist = Strategist(ledger, proposer=_ConstantProposer(params), max_attempts=5)
        seen = {proposal_fingerprint("vwap_reversion", params)}
        with pytest.raises(StrategistError, match="bounded space exhausted"):
            strategist.propose("vwap_reversion", market=AssetClass.CRYPTO, window="w", seen=seen)
        assert ledger.count(AssetClass.CRYPTO, "vwap_reversion", "w") == 0  # nothing counted


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
    assert proposal_fingerprint("x", base) != proposal_fingerprint(
        "x", {"a": 1, "b": Decimal("2.5")}
    )
    assert proposal_fingerprint("x", base) != proposal_fingerprint("y", base)  # template-sensitive
