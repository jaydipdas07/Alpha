"""The off-pod risk-officer bridge (M3.7, Workflow B) — the half that must stay off the pod.

Workflow B's first half is the **risk-officer's final confirmation**, and it runs on the research
plane, never the pod, because the holdout lives here in a no-ACL store the pod must never reach
(TEST-3). This script runs the two gates and, only when both pass, writes one ``approval_requests``
row to the Vault pod — whose INSERT triggers the on-pod ``deploy_approval`` workflow → the human
FORM → (on approve) a ``start`` command for the worker (TEST-8: pod issues; worker executes).

The two gates (both computed here, off-pod):
  1. ``evaluate_paper_run`` — forward paper-trading confirmed the backtest (``paper_eval``).
  2. ``HoldoutGate.evaluate`` — the candidate promoted on the *locked* holdout, the single
     legitimate holdout read (``research/holdout_gate``). The verdict that crosses onto the pod is
     a scalar summary only (``RiskOfficerReview.request_fields`` — never the holdout return series).

Only the summary crosses the boundary; the pod ``approval_requests`` table is granted to no agent.

--demo drives the whole pipeline with a labelled TEST strategy (origin=demo): it creates the
strategy / deployment / passed paper-run rows, runs the REAL ``evaluate_paper_run`` on synthetic
returns that genuinely clear the gate, pairs it with a clearly-labelled synthetic holdout verdict
(a real one-shot holdout read lands with the M3.0 vendor data the operator is providing), and writes
the request — so the operator can approve a test FORM end-to-end and ratify 3.GATE. Run:

    uv run python scripts/risk_officer_review.py --demo      # writes the request; FORM waits

The REAL path (a discovered survivor) reuses ``write_approval_request`` with a ``RiskOfficerReview``
built from the strategy's actual paper/backtest returns and ``HoldoutGate.evaluate`` over the gate-
only ``HoldoutStore`` — wired when M3.0 paper data exists; the pieces (``HoldoutBarsFor``,
``EngineBacktester``, ``QuantAnalyst``) are already in ``alpha_core``. This bridge does the pod I/O
so ``alpha_core`` stays free of the Lemma SDK (the same rule that keeps ccxt out of the kernel).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from alpha_core.core.enums import AssetClass
from alpha_core.data.holdout import HoldoutStore
from alpha_core.data.store import BarStore
from alpha_core.helpers.config import DiscoveryCellConfig, load_discovery_config, load_yaml
from alpha_core.research.approval import RiskOfficerReview, assemble_review
from alpha_core.research.holdout_gate import HoldoutGateResult
from alpha_core.research.paper_eval import evaluate_paper_run
from alpha_core.research.promote import (
    build_survivor_backtesters,
    make_proposal,
    parse_params,
    pick_survivor,
    reconstruct_deflation_inputs,
    review_survivor,
)
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.quant_analyst import Verdict
from alpha_core.risk.limits import load_risk_config

_ROOT = Path(__file__).resolve().parents[1]

POD = "Vault"  # the Lemma agreement: always --pod Vault (mission control)
WORKER_ID = "alpha-paper-1"  # mirrors config/paper.yaml — the deployment's worker
VENUE = "delta-testnet"


def _lemma_create(table: str, data: dict[str, object], *, pod: str) -> str:
    """Create one pod record via the Mac ``lemma`` CLI (auto-refreshing auth) and return its id.

    The JSON payload is passed as a single argv element (no shell), so no escaping hazard.
    """
    proc = subprocess.run(
        [
            "lemma",
            "--output",
            "json",
            "records",
            "create",
            table,
            "--data",
            json.dumps(data),
            "--pod",
            pod,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"`lemma records create {table}` failed:\n{proc.stderr or proc.stdout}")
    return _extract_id(proc.stdout, table)


def _extract_id(stdout: str, table: str) -> str:
    """Pull the created record's id from the CLI's JSON, tolerating a few envelope shapes."""
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"could not parse `lemma` JSON for {table}: {exc}\n{stdout[:400]}"
        ) from exc
    candidates = [obj]
    if isinstance(obj, dict):
        candidates += [obj.get(k) for k in ("data", "record", "result", "item")]
    for cand in candidates:
        if isinstance(cand, dict) and cand.get("id"):
            return str(cand["id"])
    raise SystemExit(f"no record id in `lemma` output for {table}:\n{stdout[:400]}")


def write_approval_request(
    review: RiskOfficerReview, *, deployment_id: str, strategy_id: str, pod: str = POD
) -> str:
    """Write the ``approval_requests`` row (only the scalar summary crosses the boundary; TEST-3).

    The INSERT triggers the ``deploy_approval`` workflow. Refuses to surface a candidate that did
    not clear both gates — a non-passed review must never reach the human FORM.
    """
    if not review.passed:
        raise SystemExit(
            f"review did NOT pass both gates (paper={review.paper.passed}, "
            f"holdout={review.holdout.passed}); not surfacing for approval."
        )
    fields = review.request_fields(deployment_id=deployment_id, strategy_id=strategy_id)
    return _lemma_create("approval_requests", fields, pod=pod)


def _demo_returns() -> tuple[list[float], list[float]]:
    """Two synthetic return series (>= min_obs) that genuinely clear ``evaluate_paper_run``:
    backtest Sharpe ~1.67, paper Sharpe ~1.22 → retention ~0.73 (both above the configured floors).
    A symmetric ±d alternating series has population stdev exactly d, so Sharpe = mean/d."""
    d = 0.0009
    backtest = [0.0015 + (d if i % 2 else -d) for i in range(120)]  # Sharpe 0.0015/0.0009 = 1.667
    paper = [0.0011 + (d if i % 2 else -d) for i in range(120)]  # Sharpe 0.0011/0.0009 = 1.222
    return paper, backtest


def run_demo(pod: str = POD) -> str:
    """Drive a labelled TEST strategy through the whole pipeline up to the waiting FORM.

    Creates strategy / deployment / passed-paper-run rows, builds the risk-officer review (REAL
    paper-eval on synthetic returns + a clearly-labelled synthetic holdout verdict), and writes the
    request. Returns the approval_requests id. Honest by construction: origin=demo, a TEST- name,
    and the holdout reason says so — this demonstrates the *plumbing*, not a real edge (which needs
    M3.0 vendor data, per the operator's deferral).
    """
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"TEST-ma-crossover-{ts}"

    strategy_id = _lemma_create(
        "strategies",
        {
            "name": name,
            "family": "ma_crossover",
            "market": "crypto",
            "status": "paper",
            "origin": "manual",
            "config": {"fast": 10, "slow": 30, "note": "DEMO strategy — pipeline test only"},
            "rationale": (
                "DEMO — Workflow B / 3.GATE pipeline test. Not a real edge; a real survivor + the "
                "one-shot holdout read land with M3.0 vendor data."
            ),
        },
        pod=pod,
    )
    deployment_id = _lemma_create(
        "deployments",
        {
            "strategy_id": strategy_id,
            "venue": VENUE,
            "mode": "paper",
            "status": "pending_approval",
            "worker_id": WORKER_ID,
            "capital": "1000",  # money: string-Decimal (B5)
            "risk_limits": {"max_position_usd": "1000", "note": "demo"},
        },
        pod=pod,
    )

    paper_returns, backtest_returns = _demo_returns()
    paper = evaluate_paper_run(paper_returns=paper_returns, backtest_returns=backtest_returns)
    holdout = HoldoutGateResult(
        passed=True,
        verdict=Verdict.PROMOTE,
        oos_sharpe=2.4,
        n_obs=480,
        reason=(
            "PROMOTE — DEMO pipeline test (synthetic holdout verdict; the real one-shot holdout "
            "read lands with M3.0 vendor data)"
        ),
    )
    review = assemble_review(
        strategy_name=name,
        family="ma_crossover",
        market=AssetClass.CRYPTO,
        venue=VENUE,
        paper=paper,
        holdout=holdout,
        mode="paper",
        origin="demo",
    )
    if not review.passed:  # the synthetic inputs are built to pass; guard the invariant anyway
        raise SystemExit(f"demo review unexpectedly failed: paper={paper.reason}")

    # record the passed paper-run for the audit trail / cockpit (paper-vs-backtest divergence)
    _lemma_create(
        "paper_runs",
        {
            "deployment_id": deployment_id,
            "status": "passed",
            "window": "demo",
            "metrics": paper.metrics(),
        },
        pod=pod,
    )

    request_id = write_approval_request(
        review, deployment_id=deployment_id, strategy_id=strategy_id, pod=pod
    )

    print(
        "\n".join(
            [
                "Workflow B demo: request written — the human FORM is now waiting.",
                f"  strategy_id        {strategy_id}  ({name})",
                f"  deployment_id      {deployment_id}  (pending_approval)",
                f"  approval_request   {request_id}  (gate_status=passed, origin=demo)",
                f"  paper Sharpe {paper.paper_sharpe:.3f} / backtest {paper.backtest_sharpe:.3f} "
                f"(retention {paper.sharpe_retention:.0%}); holdout PROMOTE (synthetic, demo)",
                "",
                "[You] approves the test FORM (the deploy_approval run waiting in your queue):",
                f"  lemma workflows runs waiting --pod {pod}",
                "  lemma workflows runs submit-form <run-id> "
                '--data \'{"approved": true, "notes": "3.GATE demo"}\' '
                f"--pod {pod}",
                "On approve: finalize_deployment issues a `start` command on `commands` for "
                f"{WORKER_ID} → the worker executes (TEST-8).",
            ]
        )
    )
    return request_id


def _store_root(env: str, default: str, override: str | None) -> Path:
    """A store root: an explicit ``--…-root`` override, else ``$ENV``, else the repo default."""
    if override:
        return Path(override)
    return Path(os.environ.get(env) or (_ROOT / default))


def load_survivor_from_run(
    run_path: Path, window: str, template: str | None
) -> tuple[str, dict[str, str]]:
    """Pull a survivor (family + params) from a ``discovery_runs/*.json`` record (via pick_survivor;
    ``template`` disambiguates a window with multiple promoted families)."""
    try:
        return pick_survivor(json.loads(run_path.read_text()), window=window, template=template)
    except ValueError as exc:
        raise SystemExit(f"{exc} (in {run_path})") from exc


def _render_review(review: RiskOfficerReview, *, name: str, n_trials: int) -> str:
    """A human summary of both gates (scalar verdicts only — the same TEST-3-safe figures)."""
    p, h = review.paper, review.holdout
    return "\n".join(
        [
            f"=== risk-officer review — {name} (origin={review.origin}) ===",
            f"  gate_status : {review.gate_status.upper()}  (passed={review.passed})",
            f"  HOLDOUT  verdict={h.verdict.value}  passed={h.passed}  "
            f"oos_sharpe={h.oos_sharpe:+.4f}  n_obs={h.n_obs}  (n_trials={n_trials})",
            f"           {h.reason}",
            f"  PAPER    passed={p.passed}  paper_sharpe={p.paper_sharpe:+.4f}  "
            f"backtest_sharpe={p.backtest_sharpe:+.4f}  retention={p.sharpe_retention:.0%}",
            f"           {p.reason}",
        ]
    )


def _surface_survivor(
    review: RiskOfficerReview,
    *,
    cell: DiscoveryCellConfig,
    template: str,
    params: dict[str, object],
    venue: str,
    name: str,
    capital: str | None,
    pod: str,
) -> int:
    """Create the ``strategies`` / ``deployments`` / ``paper_runs`` rows + the approval request.
    Reached ONLY when both gates passed. NB the two ``origin`` columns differ: ``strategies.origin``
    is ``constrained|freeform|manual`` (the strategist is the constrained one), while
    ``approval_requests.origin`` is ``discovery|demo`` (set on the RiskOfficerReview)."""
    strategy_id = _lemma_create(
        "strategies",
        {
            "name": name,
            "family": template,
            "market": cell.market.value.lower(),
            "status": "paper",
            "origin": "constrained",  # strategies.origin ENUM: constrained|freeform|manual
            "config": {k: str(v) for k, v in params.items()},
            "rationale": (
                f"Discovered survivor {template} {dict(params)} on "
                f"{cell.market.value}/{cell.window} — cleared the holdout + paper gates."
            ),
        },
        pod=pod,
    )
    deployment_id = _lemma_create(
        "deployments",
        {
            "strategy_id": strategy_id,
            "venue": venue,
            "mode": "paper",
            "status": "pending_approval",
            "worker_id": WORKER_ID,
            "capital": str(capital or cell.starting_cash),  # money: string-Decimal (B5)
            "risk_limits": {"note": "discovery survivor"},
        },
        pod=pod,
    )
    _lemma_create(
        "paper_runs",
        {
            "deployment_id": deployment_id,
            "status": "passed",
            "window": cell.window,
            "metrics": review.paper.metrics(),
        },
        pod=pod,
    )
    request_id = write_approval_request(
        review, deployment_id=deployment_id, strategy_id=strategy_id, pod=pod
    )
    print(
        "\n".join(
            [
                f"\nrequest written ({request_id}) — the human FORM is now waiting.",
                f"  strategy_id    {strategy_id}  ({name})",
                f"  deployment_id  {deployment_id}  (pending_approval, paper)",
                f"  [You] approve: lemma workflows runs waiting --pod {pod}",
            ]
        )
    )
    return 0


def run_survivor(args: argparse.Namespace) -> int:
    """Drive a REAL discovered survivor through both Workflow-B gates; surface to the FORM iff it
    passes. ``write_approval_request`` refuses a non-passing review, so a holdout-rejected survivor
    (band_bps=55) is correctly never surfaced — the gate working."""
    if args.from_run:
        template, raw_params = load_survivor_from_run(
            Path(args.from_run), args.window, args.template
        )
    else:
        template = args.template
        raw_params = dict(p.split("=", 1) for p in (args.params or []))
    params = parse_params(raw_params, template)
    cell = next((c for c in load_discovery_config().cells if c.window == args.window), None)
    if cell is None:
        raise SystemExit(f"no discovery cell for window {args.window!r} in config/discovery.yaml")

    research = BarStore(_store_root("ALPHA_RESEARCH_ROOT", "data_research", args.research_root))
    holdout = HoldoutStore(_store_root("ALPHA_HOLDOUT_ROOT", "data_holdout", args.holdout_root))
    ledger_path = Path(args.ledger) if args.ledger else (_ROOT / "proposal_ledger.sqlite")
    risk_config = load_risk_config()
    cost_config = load_yaml("costs.yaml")
    with ProposalLedger(ledger_path) as ledger:
        n_trials, variance = reconstruct_deflation_inputs(
            template=template,
            cell=cell,
            research_store=research,
            ledger=ledger,
            risk_config=risk_config,
            cost_config=cost_config,
        )
    in_bt, hold_bt = build_survivor_backtesters(
        cell=cell,
        research_store=research,
        holdout_store=holdout,
        risk_config=risk_config,
        cost_config=cost_config,
    )
    paper_returns = json.loads(Path(args.paper_returns_file).read_text())
    if (
        not isinstance(paper_returns, list)
        or not paper_returns
        or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in paper_returns)
    ):
        raise SystemExit("--paper-returns-file must be a non-empty JSON list of finite numbers")

    # default to the EXECUTION venue (Binance is data-only; crypto paper-trades on Delta testnet)
    venue = args.venue or (VENUE if cell.market is AssetClass.CRYPTO else "")
    if not venue:
        raise SystemExit(f"--venue is required for a {cell.market.value} survivor (no default)")
    name = (
        args.strategy_name
        or f"{template}-{cell.window}-" + "-".join(f"{k}{v}" for k, v in sorted(params.items()))
    )[:120]  # strategies.name / approval_requests.strategy_name are max_length 120
    review = review_survivor(
        proposal=make_proposal(template, params, cell, trial_index=n_trials),
        in_sample_backtester=in_bt,
        holdout_backtester=hold_bt,
        paper_returns=paper_returns,
        n_trials=n_trials,
        trial_sharpe_variance=variance,
        strategy_name=name,
        venue=venue,
        origin="discovery",
    )
    print(_render_review(review, name=name, n_trials=n_trials))
    if not review.passed:
        print(
            "\nNOT SURFACED — the review did not clear both gates (above); no approval_requests "
            "row written, nothing reaches the FORM. This is the gate working.",
            file=sys.stderr,
        )
        return 1
    return _surface_survivor(
        review,
        cell=cell,
        template=template,
        params=dict(params),
        venue=venue,
        name=name,
        capital=args.capital,
        pod=args.pod,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="drive a labelled TEST strategy through the whole pipeline up to the waiting FORM",
    )
    parser.add_argument(
        "--survivor",
        action="store_true",
        help="drive a REAL discovered survivor through both gates (holdout + paper) to the FORM",
    )
    parser.add_argument("--from-run", help="discovery_runs/*.json record to load the survivor from")
    parser.add_argument("--window", help="the survivor's cell window (e.g. avaxusdt-1d)")
    parser.add_argument("--template", help="strategy family (when not using --from-run)")
    parser.add_argument("--params", nargs="*", help="params as key=value (e.g. band_bps=55)")
    parser.add_argument("--paper-returns-file", help="JSON list of forward paper returns")
    parser.add_argument("--strategy-name", help="deployment strategy name (default: derived)")
    parser.add_argument("--venue", help="execution venue (default: the cell's venue)")
    parser.add_argument("--capital", help="deployment capital (default: the cell's starting_cash)")
    parser.add_argument("--research-root", help="research store root ($ALPHA_RESEARCH_ROOT)")
    parser.add_argument("--holdout-root", help="holdout store root ($ALPHA_HOLDOUT_ROOT)")
    parser.add_argument("--ledger", help="proposal ledger path (./proposal_ledger.sqlite)")
    parser.add_argument("--pod", default=POD, help="Lemma pod (default: Vault)")
    args = parser.parse_args(argv)

    if args.survivor:
        if not (args.window and args.paper_returns_file):
            parser.error("--survivor needs --window and --paper-returns-file")
        if not (args.from_run or args.template):
            parser.error("--survivor needs --from-run <record> or --template <family> --params …")
        return run_survivor(args)
    if args.demo:
        run_demo(args.pod)
        return 0
    parser.print_help()
    print(
        "\nPass --demo (a synthetic labelled pipeline test) or --survivor (a real discovered "
        "survivor driven through both gates). See the module docstring.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
