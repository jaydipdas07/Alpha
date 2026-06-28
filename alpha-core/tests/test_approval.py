"""Risk-officer review tests (M3.7, Workflow B) — the off-pod gate combiner + the TEST-3 boundary.

``RiskOfficerReview.request_fields`` is the only thing that crosses onto the pod (the
``approval_requests`` row). These tests pin two contracts: (1) both gates must pass for the
candidate to surface for approval, and (2) the row payload carries *only* scalar verdict summaries —
never the holdout / paper return series — so the agent-excluded table can hold the holdout-derived
verdict without any path for an agent to reconstruct the holdout data.
"""

from __future__ import annotations

import json
import math

from alpha_core.core.enums import AssetClass
from alpha_core.research.approval import RiskOfficerReview, assemble_review
from alpha_core.research.holdout_gate import HoldoutGateResult
from alpha_core.research.paper_eval import PaperEvaluation
from alpha_core.research.quant_analyst import Verdict


def _paper(passed: bool = True, ps: float = 1.2, bs: float = 1.5) -> PaperEvaluation:
    return PaperEvaluation(
        passed=passed,
        paper_sharpe=ps,
        backtest_sharpe=bs,
        sharpe_retention=(ps / bs) if bs else 0.0,
        reason="paper confirms backtest" if passed else "paper Sharpe below floor",
    )


def _holdout(passed: bool = True, verdict: Verdict = Verdict.PROMOTE) -> HoldoutGateResult:
    return HoldoutGateResult(
        passed=passed,
        verdict=verdict,
        oos_sharpe=2.4,
        n_obs=480,
        reason="promote" if passed else "no out-of-sample edge",
    )


def _review(paper: PaperEvaluation, holdout: HoldoutGateResult, **kw: object) -> RiskOfficerReview:
    return assemble_review(
        strategy_name="TEST-ma-crossover",
        family="ma_crossover",
        market=AssetClass.CRYPTO,
        venue="delta-testnet",
        paper=paper,
        holdout=holdout,
        **kw,  # type: ignore[arg-type]
    )


def test_passes_only_when_both_gates_pass() -> None:
    assert _review(_paper(True), _holdout(True)).passed is True
    assert _review(_paper(False), _holdout(True)).passed is False  # paper failed
    assert _review(_paper(True), _holdout(False)).passed is False  # holdout failed
    assert _review(_paper(False), _holdout(False)).passed is False


def test_gate_status_string_mirrors_passed() -> None:
    assert _review(_paper(True), _holdout(True)).gate_status == "passed"
    assert _review(_paper(True), _holdout(False)).gate_status == "rejected"


def test_request_fields_shape_and_lowercase_market() -> None:
    fields = _review(_paper(True), _holdout(True)).request_fields(
        deployment_id="dep-1", strategy_id="str-1"
    )
    assert fields["deployment_id"] == "dep-1"
    assert fields["strategy_id"] == "str-1"
    assert fields["gate_status"] == "passed"
    assert fields["status"] == "pending"
    # pod ENUMs are lowercase
    assert fields["market"] == "crypto"
    assert fields["holdout_verdict"] == "promote"
    # the headline scalars are present and numeric
    assert fields["paper_sharpe"] == 1.2
    assert fields["holdout_oos_sharpe"] == 2.4
    assert fields["holdout_n_obs"] == 480
    # JSON-serializable end to end (the bridge writes this to the pod verbatim)
    json.dumps(fields)


def test_request_fields_is_a_summary_only_no_return_series() -> None:
    """TEST-3 boundary: every value crossing onto the pod is a scalar / short string / small dict —
    never a sequence of floats (a return series) or bars. If this ever fails, the holdout data
    itself is leaking onto the agent-excluded table, which is exactly what must never happen."""
    fields = _review(_paper(True), _holdout(True)).request_fields(
        deployment_id="dep-1", strategy_id="str-1"
    )

    def assert_no_float_sequence(value: object, path: str) -> None:
        if isinstance(value, (list, tuple)):
            raise AssertionError(f"{path}: a sequence crossed the boundary — possible series leak")
        if isinstance(value, dict):
            for k, v in value.items():
                assert_no_float_sequence(v, f"{path}.{k}")

    for key, value in fields.items():
        assert_no_float_sequence(value, key)


def test_non_finite_sharpe_coerced_to_none() -> None:
    """A degenerate (zero-variance) Sharpe of ±inf/NaN must store as null, not crash the FLOAT
    column. Only the affected scalar is nulled; the rest of the row is intact."""
    paper = PaperEvaluation(
        passed=True,
        paper_sharpe=math.inf,
        backtest_sharpe=1.0,
        sharpe_retention=math.nan,
        reason="degenerate",
    )
    fields = _review(paper, _holdout(True)).request_fields(deployment_id="d", strategy_id="s")
    assert fields["paper_sharpe"] is None
    assert fields["sharpe_retention"] is None
    assert fields["backtest_sharpe"] == 1.0
    json.dumps(fields)  # null is JSON-safe; inf/nan would not be


def test_origin_defaults_to_discovery_and_demo_is_labelled() -> None:
    assert _review(_paper(True), _holdout(True)).origin == "discovery"
    demo = _review(_paper(True), _holdout(True), origin="demo")
    assert demo.request_fields(deployment_id="d", strategy_id="s")["origin"] == "demo"
