"""Workflow A — the discovery cycle (B1b.3b): propose -> backtest -> adjudicate -> survivors."""

from __future__ import annotations

from collections.abc import Sequence

from alpha_core.core.enums import AssetClass
from alpha_core.research.calibration import edge_population, overfit_population
from alpha_core.research.discovery import DiscoveryReport, run_discovery_cycle
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.quant_analyst import QuantAnalyst, Verdict
from alpha_core.research.strategist import RandomProposer, Strategist, StrategyProposal

_CRYPTO = AssetClass.CRYPTO


class _ScriptedBacktest:
    """A fake backtester: hands back the next prepared return series per proposal (in order)."""

    def __init__(self, series: list[list[float]]) -> None:
        self._series = iter(series)

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        return list(next(self._series))


def _cycle(
    template: str, backtester: _ScriptedBacktest, ledger: ProposalLedger, n: int
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
    assert report.survivors == [p for p, a in report.assessments if a.verdict is Verdict.PROMOTE]


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
