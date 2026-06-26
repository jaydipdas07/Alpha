"""Workflow A — the discovery cycle (B1b.3b): propose -> backtest -> adjudicate -> survivors."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from alpha_core.core.enums import AssetClass
from alpha_core.research.calibration import edge_population, noise_population, overfit_population
from alpha_core.research.discovery import Backtester, DiscoveryReport, run_discovery_cycle
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.quant_analyst import QuantAnalyst, Verdict
from alpha_core.research.strategist import (
    RandomProposer,
    Strategist,
    StrategistError,
    StrategyProposal,
)

_CRYPTO = AssetClass.CRYPTO


class _ScriptedBacktest:
    """A fake backtester: hands back the next prepared return series per proposal (in order)."""

    def __init__(self, series: list[list[float]]) -> None:
        self._series = iter(series)

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        return list(next(self._series))


def _cycle(
    template: str, backtester: Backtester, ledger: ProposalLedger, n: int
) -> DiscoveryReport:
    return run_discovery_cycle(
        template,
        market=_CRYPTO,
        window="2020-2024",
        strategist=Strategist(ledger, proposer=RandomProposer(seed=2)),
        quant_analyst=QuantAnalyst(),
        backtester=backtester,
        n_candidates=n,
    )


# --- the Done-when: a run produces promote/reject decisions end-to-end --------------------------


def test_a_discovery_run_promotes_planted_edges() -> None:
    # every candidate backtests into a planted EDGE -> the cycle promotes them (the conservative
    # gate retains strong edges), and the survivors are exactly the promoted candidates.
    edges = edge_population(n_candidates=10, n_obs=2400, seed=1, drift=0.3)
    with ProposalLedger() as ledger:
        report = _cycle("ma_crossover", _ScriptedBacktest(edges), ledger, 10)
        assert ledger.count(_CRYPTO, "ma_crossover", "2020-2024") == 10  # each proposal a trial
    assert len(report.assessments) == 10
    assert len(report.survivors) >= 8  # most strong edges promoted
    assert all(a.verdict is Verdict.PROMOTE for p, a in report.assessments if p in report.survivors)


def test_a_discovery_run_rejects_planted_overfits() -> None:
    # every candidate backtests into a planted OVERFIT (poor OOS) -> all rejected, no survivors.
    overfits = overfit_population(n_candidates=10, n_obs=2400, seed=2)
    with ProposalLedger() as ledger:
        report = _cycle("ma_crossover", _ScriptedBacktest(overfits), ledger, 10)
    assert report.survivors == []
    assert all(a.verdict is Verdict.REJECT for _, a in report.assessments)


# --- bounds: saturation + empty ----------------------------------------------------------------


def test_a_run_stops_early_when_the_cell_saturates() -> None:
    # vwap_reversion has only 19 distinct configs; asking for 100 -> the strategist saturates and
    # the cycle adjudicates what it found (it neither hangs nor proposes more than the cell holds).
    series = [[0.1 * (i % 7) for i in range(120)] for _ in range(25)]
    with ProposalLedger() as ledger:
        report = _cycle("vwap_reversion", _ScriptedBacktest(series), ledger, 100)
        assert ledger.count(_CRYPTO, "vwap_reversion", "2020-2024") == len(report.assessments)
    assert 0 < len(report.assessments) <= 19


def test_a_run_with_no_candidates_returns_an_empty_report() -> None:
    with ProposalLedger() as ledger:
        report = _cycle("ma_crossover", _ScriptedBacktest([]), ledger, 0)
    assert isinstance(report, DiscoveryReport)
    assert report.assessments == []
    assert report.survivors == []


# --- the loop genuinely pairs each proposal with its own backtest + verdict ---------------------


class _LinkedBacktest:
    """Returns a planted EDGE for proposals with ``fast_period < 17``, else NOISE — so each
    candidate's verdict is set by ITS OWN params; records every proposal it ran, to prove the loop
    pairs returns to the right proposal (not just feeds canned series in order)."""

    def __init__(self, edge: list[float], noise: list[float]) -> None:
        self._edge = edge
        self._noise = noise
        self.ran: list[StrategyProposal] = []

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        self.ran.append(proposal)
        return self._edge if int(proposal.params["fast_period"]) < 17 else self._noise


def test_the_loop_pairs_each_proposal_with_its_own_backtest() -> None:
    edge = edge_population(n_candidates=1, n_obs=2400, seed=1, drift=0.3)[0]
    noise = noise_population(n_candidates=1, n_obs=2400, seed=2)[0]
    backtester = _LinkedBacktest(edge, noise)
    with ProposalLedger() as ledger:
        report = _cycle("ma_crossover", backtester, ledger, 12)
    # the loop backtested exactly the proposed candidates, in order (no mis-zip).
    assert [p for p, _ in report.assessments] == backtester.ran
    # a candidate's OWN params drove its returns -> its verdict (the real linkage): a proposal is a
    # survivor iff promoted, and every survivor earned the edge via fast_period < 17.
    for proposal, assessment in report.assessments:
        assert (proposal in report.survivors) == (assessment.verdict is Verdict.PROMOTE)
    assert report.survivors  # the linkage is actually exercised (some edge candidate promoted)
    assert all(int(p.params["fast_period"]) < 17 for p in report.survivors)


def test_a_second_cycle_deflates_by_the_cumulative_count(tmp_path: Path) -> None:
    # the desync guard: a second cycle over the same durable cell deflates by the CUMULATIVE trial
    # count (not just this run's 5), so it can never under-penalize on a re-run.
    db = tmp_path / "proposals.db"
    run1 = edge_population(n_candidates=5, n_obs=2400, seed=1, drift=0.3)
    run2 = edge_population(n_candidates=5, n_obs=2400, seed=9, drift=0.3)
    with ProposalLedger(db) as ledger:
        _cycle("ma_crossover", _ScriptedBacktest(run1), ledger, 5)  # cell now holds 5 trials
    with ProposalLedger(db) as ledger:
        report = _cycle(
            "ma_crossover", _ScriptedBacktest(run2), ledger, 5
        )  # 5 more (cumulative 10)
        assert ledger.count(_CRYPTO, "ma_crossover", "2020-2024") == 10
    assert all(a.n_trials == 10 for _, a in report.assessments)  # cumulative (10), not this run's 5


def test_an_unknown_template_or_invalid_cell_propagates_not_swallowed() -> None:
    # only a saturated cell is swallowed; a deterministic caller bug must raise, not return empty.
    with ProposalLedger() as ledger:
        with pytest.raises(StrategistError, match="unknown template"):
            _cycle("no_such_template", _ScriptedBacktest([]), ledger, 5)
        with pytest.raises(StrategistError, match="invalid cell"):
            run_discovery_cycle(
                "ma_crossover",
                market=_CRYPTO,
                window="2020|2024",  # the '|' delimiter -> invalid cell
                strategist=Strategist(ledger),
                quant_analyst=QuantAnalyst(),
                backtester=_ScriptedBacktest([]),
                n_candidates=5,
            )
