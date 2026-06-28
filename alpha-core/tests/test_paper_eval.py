"""Paper-run evaluator tests (M3.7)."""

from __future__ import annotations

from alpha_core.helpers.config import PaperEvalConfig
from alpha_core.research.paper_eval import evaluate_paper_run

CFG = PaperEvalConfig(min_paper_sharpe=0.5, min_sharpe_retention=0.5)


def _series(mean: float, spread: float, n: int = 100) -> list[float]:
    # alternating mean±spread -> mean=mean, population stdev=spread -> Sharpe = mean/spread.
    return [mean + spread, mean - spread] * (n // 2)


def test_paper_confirms_backtest() -> None:
    res = evaluate_paper_run(
        paper_returns=_series(0.018, 0.01),  # Sharpe ~1.8
        backtest_returns=_series(0.02, 0.01),  # Sharpe ~2.0
        config=CFG,
    )
    assert res.passed
    assert res.paper_sharpe > 1.5 and 0.85 < res.sharpe_retention < 1.0
    assert res.metrics()["paper_sharpe"] == res.paper_sharpe


def test_paper_below_absolute_floor_fails() -> None:
    res = evaluate_paper_run(
        paper_returns=_series(0.002, 0.01),  # Sharpe ~0.2 < 0.5 floor
        backtest_returns=_series(0.02, 0.01),
        config=CFG,
    )
    assert res.passed is False and "floor" in res.reason


def test_paper_edge_collapsed_fails_retention() -> None:
    # Paper Sharpe clears the floor (0.8 > 0.5) but kept < half the backtest edge (0.8/3).
    res = evaluate_paper_run(
        paper_returns=_series(0.008, 0.01),  # Sharpe ~0.8
        backtest_returns=_series(0.03, 0.01),  # Sharpe ~3.0
        config=CFG,
    )
    assert res.passed is False and "kept only" in res.reason


def test_no_backtest_edge_passes_on_paper_alone() -> None:
    # The backtest had no positive Sharpe -> retention is undefined -> 1.0 if paper is positive.
    res = evaluate_paper_run(
        paper_returns=_series(0.02, 0.01),  # Sharpe ~2.0 > floor
        backtest_returns=_series(-0.01, 0.01),  # Sharpe ~-1.0
        config=CFG,
    )
    assert res.passed and res.sharpe_retention == 1.0


def test_default_config_loads_from_rigor_yaml() -> None:
    res = evaluate_paper_run(
        paper_returns=_series(0.02, 0.01), backtest_returns=_series(0.02, 0.01)
    )  # config=None -> PaperEvalConfig() defaults (mirrors config/rigor.yaml)
    assert res.passed
